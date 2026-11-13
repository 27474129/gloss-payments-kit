"""MAX messenger bridge.

Thin endpoints for the MAX bot. They resolve a MAX identity to a user and then
reuse the same repos / services / background tasks as the Telegram routes — the
orchestration is duplicated on purpose while the shared core is still being
extracted (PLAN.md Track B); the money and generation logic underneath is shared.

Every user-facing action keys on the MAX ``external_id`` (the MAX ``user_id``),
never a Telegram id.
"""
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks, Request, UploadFile
from pydantic import BaseModel

from app.api.deps import AuthDep, get_db
from app.core.config import settings
from app.db import (
    channel_rewards_repo, photos_repo, pricing_repo, referrals_repo, trends_repo, users_repo, yookassa_repo,
)
from app.services import s3, photo_task
from app.services import yookassa as yookassa_service
from app.services import bot_pay, payment_gateway, tbank_acq  # --- T-BANK ---
from app.services import max_bot
from app.services import first_purchase_bonus
from app.services import discount_ladder, gift_push, h6_media, h6_model
from app.services import photo_rating as photo_rating_service
from app.services import result_edit
from app.services.analytics import analytics, analytics_user_id
from app.services.notifications_client import cancel_event, fire_event_bg, notifications_client_for
from app.services.referral_notify import notify_referrer
from app.services.onboarding_gate import require_onboarding_finished
from app.services.photo_validator import validate_photo
from app.api.routes.photos import (
    ALLOWED_CONTENT_TYPES,
    MAX_FILE_SIZE,
    PHOTO_ANIMATION_COST,
    UNIFIED_PROMPT_GENERATION_COST,
    _resolve_trend_prompt,
    analyze_copy_look_reference,
    get_onboarding_preset,
    require_copy_look_generation,
)
from app.services import copy_look

router = APIRouter()
logger = logging.getLogger(__name__)

PLATFORM = "max"
MAX_BONUS_PLATFORM = "max_bonus"
MAX_ONBOARDING_TRIAL_TIER = {"generations": 10, "rubles": 299}


def _analytics_id(external_id: str) -> str:
    return analytics_user_id(PLATFORM, external_id)


async def _resolve_user(pool: asyncpg.Pool, external_id: str) -> dict:
    user_id = await users_repo.resolve_user_by_identity(pool, PLATFORM, external_id)
    if user_id is None:
        raise HTTPException(status_code=404, detail="User not found")
    data = await users_repo.get_user_by_id(pool, user_id)
    if data is None:
        raise HTTPException(status_code=404, detail="User not found")
    return data


def _require_feature_access(user_data: dict) -> None:
    """Reserve a new MAX user's starter generation for onboarding."""
    require_onboarding_finished(user_data)


# --- identity ------------------------------------------------------------------

class MaxRegisterRequest(BaseModel):
    external_id: str
    username: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    source: str | None = None
    # Yandex Metrika ClientID from a yd_<source>_<ClientID> link; stored only when the account is created.
    metrika_client_id: str | None = None
    # Short code of a VK ad click from a vc_<source>_<code> link (vk_clicks); stored only when the account is created.
    vk_click_code: str | None = None


@router.post("/register", dependencies=[AuthDep])
async def register(
    body: MaxRegisterRequest, request: Request, pool: asyncpg.Pool = Depends(get_db),
):
    result = await users_repo.get_or_create_user_by_identity(
        pool, PLATFORM, body.external_id,
        username=body.username, first_name=body.first_name,
        last_name=body.last_name, source=body.source,
        metrika_client_id=body.metrika_client_id,
        vk_click_code=body.vk_click_code,
    )
    user = result["user"]
    h6_group = user.pop("h6_assigned", None)
    if h6_group:
        await h6_model.track_assigned(user["id"], h6_group, PLATFORM, body.external_id)
    try:
        recipient = int(body.external_id)
    except (TypeError, ValueError):
        recipient = None
    if user.get("is_new"):
        await analytics.track(
            "user_registered",
            user_id=_analytics_id(body.external_id),
            properties={"platform": PLATFORM, "source": users_repo.analytics_source(body.source)},
        )
        referrer_id = user.get("referrer_user_id")
        if referrer_id is not None:
            await analytics.track(
                "referral_bound",
                user_id=_analytics_id(body.external_id),
                properties={"platform": PLATFORM, "referrer_user_id": referrer_id},
            )
        if recipient is not None:
            fire_event_bg(request, "user_registered", recipient, {
                "first_name": body.first_name or "",
                "username": body.username or "",
            }, platform=PLATFORM)
            fire_event_bg(request, "onboarding_started", recipient, {
                "first_name": body.first_name or "",
            }, platform=PLATFORM)
    elif recipient is not None and user.get("profile_photo_id") is not None:
        # A returning user who already has a face on file is past onboarding —
        # drop any reminder that is still scheduled.
        await cancel_event(request, "onboarding_completed", recipient, platform=PLATFORM)
    return result


class MaxDialogRequest(BaseModel):
    external_id: str
    chat_id: int


@router.put("/dialog", dependencies=[AuthDep])
async def set_dialog(body: MaxDialogRequest, pool: asyncpg.Pool = Depends(get_db)):
    """The bot reports the MAX dialog chat id of a user (see services.max_dialogs)."""
    changed = await users_repo.set_identity_dialog_chat_id(pool, PLATFORM, body.external_id, body.chat_id)
    if changed is None:
        raise HTTPException(status_code=404, detail="User not found")
    return {"changed": changed}


@router.get("/me", dependencies=[AuthDep])
async def me(external_id: str, pool: asyncpg.Pool = Depends(get_db)):
    return await _resolve_user(pool, external_id)


@router.get("/referral", dependencies=[AuthDep])
async def referral(external_id: str, pool: asyncpg.Pool = Depends(get_db)):
    user = await _resolve_user(pool, external_id)
    return await referrals_repo.get_summary(pool, user["user"]["id"])


# --- profile ------------------------------------------------------------------

class MaxGenderRequest(BaseModel):
    external_id: str
    gender: str


class MaxAspectRatioRequest(BaseModel):
    external_id: str
    aspect_ratio: str


@router.put("/profile/gender", dependencies=[AuthDep])
async def set_gender(body: MaxGenderRequest, pool: asyncpg.Pool = Depends(get_db)):
    gender = body.gender.lower()
    if gender not in {"female", "male"}:
        raise HTTPException(status_code=400, detail="Unsupported gender")
    user = (await _resolve_user(pool, body.external_id))["user"]
    await pool.execute(
        "UPDATE users SET gender = $1, updated_at = NOW() WHERE id = $2",
        gender, user["id"],
    )
    return {"ok": True, "gender": gender}


@router.put("/profile/aspect-ratio", dependencies=[AuthDep])
async def set_preferred_aspect_ratio(
    body: MaxAspectRatioRequest, pool: asyncpg.Pool = Depends(get_db),
):
    if body.aspect_ratio not in {"3:4", "1:1"}:
        raise HTTPException(status_code=400, detail="Unsupported aspect ratio")
    user = (await _resolve_user(pool, body.external_id))["user"]
    await users_repo.set_preferred_aspect_ratio(pool, user["id"], body.aspect_ratio)
    await analytics.track(
        "preferred_aspect_ratio_changed",
        user_id=_analytics_id(body.external_id),
        properties={"aspect_ratio": body.aspect_ratio, "platform": PLATFORM},
    )
    return {"ok": True, "aspect_ratio": body.aspect_ratio}


class MaxProfilePhotoRequest(BaseModel):
    external_id: str
    photo_id: int


async def _set_max_profile_photo(
    body: MaxProfilePhotoRequest, pool: asyncpg.Pool, *, kind: str,
) -> dict:
    user = (await _resolve_user(pool, body.external_id))["user"]
    photo = await photos_repo.get_photo(pool, body.photo_id)
    if not photo or photo["user_id"] != user["id"]:
        raise HTTPException(status_code=404, detail="Photo not found")
    setters = {
        "photo": users_repo.set_profile_photo,
        "additional-photo": users_repo.set_additional_photo,
        "child-photo": users_repo.set_child_photo,
    }
    await setters[kind](pool, user["id"], body.photo_id)
    return {"ok": True, "photo_id": body.photo_id}


@router.put("/profile/photo", dependencies=[AuthDep])
async def set_profile_photo(body: MaxProfilePhotoRequest, pool: asyncpg.Pool = Depends(get_db)):
    return await _set_max_profile_photo(body, pool, kind="photo")


@router.put("/profile/additional-photo", dependencies=[AuthDep])
async def set_additional_photo(body: MaxProfilePhotoRequest, pool: asyncpg.Pool = Depends(get_db)):
    return await _set_max_profile_photo(body, pool, kind="additional-photo")


@router.put("/profile/child-photo", dependencies=[AuthDep])
async def set_child_photo(body: MaxProfilePhotoRequest, pool: asyncpg.Pool = Depends(get_db)):
    return await _set_max_profile_photo(body, pool, kind="child-photo")


# --- source photo -------------------------------------------------------------

@router.post("/photos/upload", dependencies=[AuthDep])
async def upload_photo(
    external_id: str,
    file: UploadFile,
    pool: asyncpg.Pool = Depends(get_db),
):
    if file.content_type not in ALLOWED_CONTENT_TYPES:
        raise HTTPException(status_code=400, detail="Only JPEG, PNG, WEBP allowed")
    file_bytes = await file.read()
    if len(file_bytes) > MAX_FILE_SIZE:
        raise HTTPException(status_code=400, detail="File too large (max 10MB)")

    user = (await _resolve_user(pool, external_id))["user"]

    if settings.PHOTO_VALIDATION_ENABLED:
        import asyncio
        validation = await asyncio.to_thread(validate_photo, file_bytes)
        if not validation.ok:
            await analytics.track(
                "photo_validation_failed",
                user_id=_analytics_id(external_id),
                properties={"errors": validation.errors, "platform": PLATFORM},
            )
            raise HTTPException(status_code=400, detail={
                "message": "Фото не прошло валидацию", "errors": validation.errors,
            })

    import uuid
    tmp_key = f"uploads/{user['id']}/{uuid.uuid4()}.jpg"
    await s3.upload_photo(file_bytes, tmp_key, content_type=file.content_type)
    photo_id = await photos_repo.create_photo(
        pool, user_id=user["id"], s3_key=tmp_key, file_size=len(file_bytes),
    )
    real_key = f"uploads/{user['id']}/{photo_id}.jpg"
    await s3.upload_photo(file_bytes, real_key, content_type=file.content_type)
    await photos_repo.update_photo_s3_key(pool, photo_id, real_key)

    await analytics.track(
        "photo_upload_success",
        user_id=_analytics_id(external_id),
        properties={"photo_id": photo_id, "platform": PLATFORM},
    )
    return {"photo_id": photo_id}


@router.post("/photos/upload-raw", dependencies=[AuthDep])
async def upload_raw_photo(
    external_id: str,
    file: UploadFile,
    pool: asyncpg.Pool = Depends(get_db),
):
    """Store a reference image without changing the user's profile photo."""
    if file.content_type not in ALLOWED_CONTENT_TYPES:
        raise HTTPException(status_code=400, detail="Only JPEG, PNG, WEBP allowed")
    file_bytes = await file.read()
    if len(file_bytes) > MAX_FILE_SIZE:
        raise HTTPException(status_code=400, detail="File too large (max 10MB)")
    user = (await _resolve_user(pool, external_id))["user"]
    import uuid
    temporary_key = f"uploads/{user['id']}/{uuid.uuid4()}.jpg"
    await s3.upload_photo(file_bytes, temporary_key, content_type=file.content_type)
    photo_id = await photos_repo.create_photo(
        pool, user_id=user["id"], s3_key=temporary_key, file_size=len(file_bytes),
    )
    real_key = f"uploads/{user['id']}/{photo_id}.jpg"
    await s3.upload_photo(file_bytes, real_key, content_type=file.content_type)
    await photos_repo.update_photo_s3_key(pool, photo_id, real_key)
    return {"photo_id": photo_id}


@router.get("/trends/{trend_id}", dependencies=[AuthDep])
async def get_trend(
    trend_id: int, h6_user_id: int | None = None, pool: asyncpg.Pool = Depends(get_db),
) -> dict:
    trend = await trends_repo.get_active_trend(pool, trend_id)
    if not trend:
        raise HTTPException(status_code=404, detail="Trend not found")
    # H6: with h6_user_id the preview pictures follow the group of that user (without it: originals).
    variant = await h6_media.variant_and_map(pool, h6_user_id)
    h6_media.resolve_rows([trend], variant, *h6_media.TREND_IMAGE_FIELDS)
    return trend


class MaxTrendGenerateRequest(BaseModel):
    external_id: str
    photo_ids: list[int]
    inputs: dict[str, Any] = {}
    operation_key: str | None = None


@router.post("/trends/{trend_id}/generate", dependencies=[AuthDep])
async def generate_trend(
    trend_id: int,
    body: MaxTrendGenerateRequest,
    background_tasks: BackgroundTasks,
    pool: asyncpg.Pool = Depends(get_db),
):
    user_data = await _resolve_user(pool, body.external_id)
    user = user_data["user"]
    _require_feature_access(user_data)
    trend = await trends_repo.get_active_trend(pool, trend_id)
    if not trend:
        raise HTTPException(status_code=404, detail="Trend not found")
    parameters = trend.get("parameters") or {}
    photo_count = parameters.get("photo_count", 1)
    if not isinstance(photo_count, int) or photo_count < 1 or photo_count > 3:
        raise HTTPException(status_code=409, detail="Trend photo count is configured incorrectly")
    if len(body.photo_ids) != photo_count or len(set(body.photo_ids)) != photo_count:
        raise HTTPException(status_code=400, detail=f"Provide exactly {photo_count} distinct photos")
    for photo_id in body.photo_ids:
        photo = await photos_repo.get_photo(pool, photo_id)
        if not photo or photo["user_id"] != user["id"]:
            raise HTTPException(status_code=400, detail=f"Photo {photo_id} not found")

    prompt = _resolve_trend_prompt(trend, body.inputs)
    cost = trend.get("cost_generations")
    if not isinstance(cost, int) or cost <= 0:
        raise HTTPException(status_code=409, detail="Trend price is configured incorrectly")
    try:
        task_id, created = await photos_repo.create_max_trend_generation_task(
            pool,
            user_id=user["id"],
            external_id=int(body.external_id),
            trend_id=trend_id,
            photo_ids=body.photo_ids,
            prompt=prompt,
            charged_generations=cost,
            operation_key=body.operation_key,
        )
    except ValueError:
        raise HTTPException(status_code=402, detail="Insufficient balance")
    if created:
        background_tasks.add_task(photo_task.process_trend_generation, task_id, pool)
        await analytics.track(
            "effect_generation_started",
            user_id=_analytics_id(body.external_id),
            properties={
                "task_id": task_id, "effect_id": trend_id, "effect_slug": trend["slug"],
                "photo_count": photo_count, "charged_generations": cost, "platform": PLATFORM,
            },
        )
    return {"task_id": task_id, "status": "pending", "charged_generations": cost}


class MaxPromptGenerateRequest(BaseModel):
    external_id: str
    prompt: str
    photo_ids: list[int]
    operation_key: str | None = None


class MaxPhotoRatingRequest(BaseModel):
    external_id: str
    generation_task_id: int
    score: int  # 1..5
    result_id: int | None = None
    context: str | None = None  # onboarding | photosession | trend


@router.post("/photo-rating", dependencies=[AuthDep])
async def photo_rating(body: MaxPhotoRatingRequest, pool: asyncpg.Pool = Depends(get_db)):
    """Save the 1-5 rating under a result (a second call changes it)."""
    user_data = await _resolve_user(pool, body.external_id)
    return await photo_rating_service.rate_photo(
        pool, user_id=user_data["user"]["id"], platform=PLATFORM,
        analytics_id=_analytics_id(body.external_id),
        generation_task_id=body.generation_task_id, score=body.score,
        result_id=body.result_id, context=body.context,
    )


class MaxResultAsPhotoRequest(BaseModel):
    external_id: str
    generation_task_id: int
    result_id: int | None = None


@router.post("/photos/result-as-photo", dependencies=[AuthDep])
async def result_as_photo_route(body: MaxResultAsPhotoRequest, pool: asyncpg.Pool = Depends(get_db)):
    """«✏️ Отредактировать»: copy the user's finished frame into a photo to edit; returns its ``photo_id``."""
    user_data = await _resolve_user(pool, body.external_id)
    photo_id = await result_edit.result_as_photo(
        pool, user_id=user_data["user"]["id"], generation_task_id=body.generation_task_id, result_id=body.result_id,
    )
    return {"photo_id": photo_id}


@router.post("/photos/generate-prompt", dependencies=[AuthDep])
async def generate_prompt(
    body: MaxPromptGenerateRequest,
    background_tasks: BackgroundTasks,
    pool: asyncpg.Pool = Depends(get_db),
):
    user_data = await _resolve_user(pool, body.external_id)
    user = user_data["user"]
    _require_feature_access(user_data)
    if not body.prompt.strip() or len(body.prompt) < 3:
        raise HTTPException(status_code=400, detail="Prompt must be at least 3 characters")
    if len(body.photo_ids) < 1 or len(body.photo_ids) > 3:
        raise HTTPException(status_code=400, detail="Provide 1 to 3 photos")
    for photo_id in body.photo_ids:
        photo = await photos_repo.get_photo(pool, photo_id)
        if not photo or photo["user_id"] != user["id"]:
            raise HTTPException(status_code=400, detail=f"Photo {photo_id} not found")
    try:
        task_id, created = await photos_repo.create_max_generation_task(
            pool,
            user_id=user["id"],
            external_id=int(body.external_id),
            photo_ids=body.photo_ids,
            custom_prompt=body.prompt,
            task_type="new_look",
            charged_generations=UNIFIED_PROMPT_GENERATION_COST,
            operation_key=body.operation_key,
        )
    except ValueError:
        raise HTTPException(status_code=402, detail="Insufficient balance")
    if created:
        background_tasks.add_task(photo_task.process_prompt_generation, task_id, pool)
        await analytics.track(
            "prompt_generation_started", user_id=_analytics_id(body.external_id),
            properties={"task_id": task_id, "prompt": body.prompt[:255],
                        "photo_count": len(body.photo_ids), "platform": PLATFORM},
        )
    return {"task_id": task_id, "status": "pending", "charged_generations": UNIFIED_PROMPT_GENERATION_COST}


class MaxCopyLookAnalyzeRequest(BaseModel):
    external_id: str
    photo_id: int


class MaxCopyLookGenerateRequest(BaseModel):
    external_id: str
    reference_id: int
    photo_ids: list[int]
    operation_key: str | None = None


@router.post("/photos/copy-look/analyze", dependencies=[AuthDep])
async def analyze_copy_look(body: MaxCopyLookAnalyzeRequest, pool: asyncpg.Pool = Depends(get_db)):
    user_data = await _resolve_user(pool, body.external_id)
    _require_feature_access(user_data)
    result = await analyze_copy_look_reference(pool, user_data, body.photo_id)
    await analytics.track(
        "copy_look_reference_analyzed", user_id=_analytics_id(body.external_id),
        properties={**{k: result[k] for k in ("reference_id", "photo_count", "reference_type")},
                    "platform": PLATFORM},
    )
    return result


@router.post("/photos/copy-look/generate", dependencies=[AuthDep])
async def generate_copy_look(
    body: MaxCopyLookGenerateRequest,
    background_tasks: BackgroundTasks,
    pool: asyncpg.Pool = Depends(get_db),
):
    user_data = await _resolve_user(pool, body.external_id)
    user = user_data["user"]
    _require_feature_access(user_data)
    reference, prompt = await require_copy_look_generation(pool, user, body.reference_id, body.photo_ids)
    try:
        task_id, created = await photos_repo.create_max_generation_task(
            pool,
            user_id=user["id"],
            external_id=int(body.external_id),
            photo_ids=body.photo_ids,
            custom_prompt=prompt,
            task_type="copy_look",
            charged_generations=copy_look.COPY_LOOK_COST,
            operation_key=body.operation_key,
            copy_look_reference_id=reference["id"],
        )
    except ValueError:
        raise HTTPException(status_code=402, detail="Insufficient balance")
    if created:
        background_tasks.add_task(photo_task.process_copy_look_generation, task_id, pool)
        await analytics.track(
            "copy_look_generation_started", user_id=_analytics_id(body.external_id),
            properties={"task_id": task_id, "reference_id": reference["id"],
                        "photo_count": reference["people_count"],
                        "charged_generations": copy_look.COPY_LOOK_COST, "platform": PLATFORM},
        )
    return {"task_id": task_id, "status": "pending", "charged_generations": copy_look.COPY_LOOK_COST}


class MaxUpscaleRequest(BaseModel):
    external_id: str
    photo_id: int
    operation_key: str | None = None


@router.post("/photos/upscale", dependencies=[AuthDep])
async def upscale_photo(
    body: MaxUpscaleRequest,
    background_tasks: BackgroundTasks,
    pool: asyncpg.Pool = Depends(get_db),
):
    user_data = await _resolve_user(pool, body.external_id)
    user = user_data["user"]
    _require_feature_access(user_data)
    photo = await photos_repo.get_photo(pool, body.photo_id)
    if not photo or photo["user_id"] != user["id"]:
        raise HTTPException(status_code=400, detail="Photo not found")
    try:
        task_id, created = await photos_repo.create_max_generation_task(
            pool,
            user_id=user["id"],
            external_id=int(body.external_id),
            photo_ids=[body.photo_id],
            custom_prompt="upscale",
            task_type="upscale",
            charged_generations=photo_task.ENHANCE_PHOTO_COST,
            operation_key=body.operation_key,
        )
    except ValueError:
        raise HTTPException(status_code=402, detail="Insufficient balance")
    if created:
        background_tasks.add_task(photo_task.process_upscale, task_id, pool)
        await analytics.track(
            "upscale_started", user_id=_analytics_id(body.external_id),
            properties={"task_id": task_id, "photo_id": body.photo_id, "platform": PLATFORM},
        )
    return {"task_id": task_id, "status": "pending", "charged_generations": photo_task.ENHANCE_PHOTO_COST}


class MaxAnimateRequest(BaseModel):
    external_id: str
    photo_id: int
    prompt: str
    operation_key: str | None = None


@router.post("/photos/animate", dependencies=[AuthDep])
async def animate_photo(
    body: MaxAnimateRequest,
    background_tasks: BackgroundTasks,
    pool: asyncpg.Pool = Depends(get_db),
):
    """Animate a MAX user's chosen photo into a short video."""
    if not settings.VIDEO_FEATURES_ENABLED:
        raise HTTPException(status_code=503, detail="Video features are disabled")
    user_data = await _resolve_user(pool, body.external_id)
    user = user_data["user"]
    _require_feature_access(user_data)
    prompt = body.prompt.strip()
    if not 3 <= len(prompt) <= 1000:
        raise HTTPException(status_code=400, detail="Prompt must be between 3 and 1000 characters")

    photo = await photos_repo.get_photo(pool, body.photo_id)
    if not photo or photo["user_id"] != user["id"]:
        raise HTTPException(status_code=400, detail="Photo not found")
    try:
        task_id, created = await photos_repo.create_max_generation_task(
            pool,
            user_id=user["id"],
            external_id=int(body.external_id),
            photo_ids=[body.photo_id],
            custom_prompt=prompt,
            task_type="photo_animation",
            charged_generations=PHOTO_ANIMATION_COST,
            operation_key=body.operation_key,
        )
    except ValueError:
        raise HTTPException(status_code=402, detail="Insufficient balance")
    if created:
        background_tasks.add_task(photo_task.process_photo_animation, task_id, pool)
        await analytics.track(
            "photo_animation_started", user_id=_analytics_id(body.external_id),
            properties={"task_id": task_id, "photo_id": body.photo_id,
                        "prompt": prompt[:255], "platform": PLATFORM},
        )
    return {"task_id": task_id, "status": "pending", "charged_generations": PHOTO_ANIMATION_COST}


# --- onboarding generation ---------------------------------------------------

class MaxOnboardingGenerateRequest(BaseModel):
    external_id: str
    gender: str | None = None
    operation_key: str | None = None


@router.post("/photos/generate-onboarding", dependencies=[AuthDep])
async def generate_onboarding(
    body: MaxOnboardingGenerateRequest,
    background_tasks: BackgroundTasks,
    pool: asyncpg.Pool = Depends(get_db),
):
    user = (await _resolve_user(pool, body.external_id))["user"]

    profile_photo_id = user.get("profile_photo_id")
    if not profile_photo_id:
        raise HTTPException(status_code=400, detail="Profile photo not set")

    gender = (body.gender or user.get("gender") or "female").lower()
    if gender not in {"female", "male"}:
        raise HTTPException(status_code=400, detail="Unsupported onboarding gender")
    if user.get("gender") != gender:
        await pool.execute(
            "UPDATE users SET gender = $1, updated_at = NOW() WHERE id = $2",
            gender, user["id"],
        )

    preset, experiment_name = await get_onboarding_preset(pool, user["id"], gender)
    if not preset:
        raise HTTPException(status_code=404, detail="No preset available for onboarding")

    try:
        task_id, created = await photos_repo.create_max_generation_task(
            pool,
            user_id=user["id"],
            external_id=int(body.external_id),
            photo_ids=[profile_photo_id],
            task_type="onboarding",
            generation_mode="regular",
            charged_generations=1,
            preset_id=preset["id"],
            operation_key=body.operation_key,
        )
    except ValueError:
        raise HTTPException(status_code=402, detail="Insufficient balance")
    if created:
        background_tasks.add_task(photo_task.process_random_generation, task_id, pool)
        await analytics.track(
            "onboarding_generation_started",
            user_id=_analytics_id(body.external_id),
            properties={"task_id": task_id, "preset_id": preset["id"], "gender": gender,
                        "experiment_name": experiment_name, "platform": PLATFORM},
        )
    return {"task_id": task_id, "status": "pending", "charged_generations": 1}


# --- channel subscription ----------------------------------------------------

CHANNEL_REWARD_SOURCES = {"onboarding", "menu"}


@router.get("/channel", dependencies=[AuthDep])
async def channel_status(external_id: str, pool: asyncpg.Pool = Depends(get_db)):
    """Whether the onboarding gate is on and this user is done with the channel.

    The bot shows its menu «+1 за подписку» button while ``claimed`` is false.
    That reward is off (MAX requires approval for promotions), so onboarded
    users always read as claimed; the onboarding gate only runs before
    onboarding is completed and still sees the real value.
    """
    user = (await _resolve_user(pool, external_id))["user"]
    gate = await pricing_repo.get_value(pool, "max_channel_gate_enabled")
    claimed = await channel_rewards_repo.has_claimed(pool, user["id"])
    return {
        "gate_enabled": (gate or "").strip().lower() in {"1", "true", "yes", "on"},
        "claimed": bool(claimed) or bool(user.get("onboarding_completed")),
    }


class MaxChannelClaimRequest(BaseModel):
    external_id: str
    source: str


@router.post("/channel/claim", dependencies=[AuthDep])
async def channel_claim(body: MaxChannelClaimRequest, pool: asyncpg.Pool = Depends(get_db)):
    """Record a verified channel subscription.  It never credits generations.

    The bot checks membership with the MAX API before calling this.  The
    onboarding gate unlocks the starter generation; the old menu «+1» bonus
    was switched off on 2026-10-14 (MAX requires approval for promotions),
    so a press on an old «Я подписался» button just records the row.
    """
    if body.source not in CHANNEL_REWARD_SOURCES:
        raise HTTPException(status_code=400, detail="Unsupported source")
    user = (await _resolve_user(pool, body.external_id))["user"]
    credit = 0
    inserted, remaining = await channel_rewards_repo.claim(
        pool, user["id"], platform=PLATFORM, source=body.source, credit=credit,
    )
    granted = credit if inserted else 0
    if inserted:
        await analytics.track(
            "channel_subscription_rewarded",
            user_id=_analytics_id(body.external_id),
            properties={"source": body.source, "generations": granted, "platform": PLATFORM},
        )
    return {
        "granted": granted,
        "already_claimed": not inserted,
        "generations_remaining": remaining,
    }


# --- paywall + pricing -------------------------------------------------------

class MaxExternalRequest(BaseModel):
    external_id: str


class MaxPaywallShownRequest(MaxExternalRequest):
    # The bot sets it while onboarding v2 is on; it only marks the analytics event (before/after counts).
    onboarding_v2: bool = False


@router.post("/paywall-shown", dependencies=[AuthDep])
async def paywall_shown(
    body: MaxPaywallShownRequest, request: Request, pool: asyncpg.Pool = Depends(get_db),
):
    """Start the descending onboarding price ladder for this user."""
    user = (await _resolve_user(pool, body.external_id))["user"]
    first_offer = await pool.execute(
        "UPDATE users SET payment_offered_at = NOW() "
        "WHERE id = $1 AND payment_offered_at IS NULL AND has_purchased = FALSE",
        user["id"],
    )
    await analytics.track(
        "onboarding_paywall_shown",
        user_id=_analytics_id(body.external_id),
        properties={"platform": PLATFORM, **({"onboarding_v2": True} if body.onboarding_v2 else {})},
    )
    try:
        recipient = int(body.external_id)
    except (TypeError, ValueError):
        recipient = None
    if recipient is not None:
        # Onboarding is over the moment the paywall is shown: stop its reminders
        # and start the paywall ladder's own.
        await cancel_event(request, "onboarding_completed", recipient, platform=PLATFORM)
        # The reminder ladder has one flow instance. Re-opening a paywall must
        # not enqueue another identical 2h/24h pair.
        if not user.get("has_purchased") and first_offer.endswith("1"):
            event_name, extra = await gift_push.paywall_event(
                pool, user_id=user["id"], platform=PLATFORM, chat_id=recipient, gender=user.get("gender"),
            )
            fire_event_bg(request, event_name, recipient, {
                "first_name": user.get("first_name") or "",
                **extra,
            }, platform=PLATFORM)
    return {"ok": True}


# The initial ordinary offer stays available for three days.  The bonus
# window itself lives in app.services.first_purchase_bonus; keep the bot's
# client-side copy in core/flows/menu.py in sync with it.
ONBOARDING_BONUS_WINDOW = timedelta(days=3)


def _onboarding_trial_available(user: dict) -> bool:
    """A new user may see the initial ordinary paywall for three days."""
    if user.get("has_purchased"):
        return False
    created_at = user.get("created_at")
    if created_at is None:
        return True
    if isinstance(created_at, str):
        try:
            created_at = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        except ValueError:
            return False
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - created_at <= ONBOARDING_BONUS_WINDOW


def _onboarding_bonus_available(user: dict) -> bool:
    """The first-purchase bonus is open from 2 to 26 hours after the paywall.

    Authoritative for both price lookup and checkout creation, so an early
    menu tap, an expired push or a forged bonus context cannot get the bonus.
    """
    return first_purchase_bonus.bonus_window_open(user)


async def _max_onboarding_tiers(pool: asyncpg.Pool, context: str) -> list[dict]:
    if context == "onboarding_trial":
        return [dict(MAX_ONBOARDING_TRIAL_TIER)]
    if context in {"onboarding_bonus", "onboarding_paywall"}:
        # Same base-price (never discounted) packages as Telegram.
        return await pricing_repo.get_first_purchase_bonus_tiers(pool)
    raise HTTPException(status_code=400, detail="Invalid payment context")


def _offer_slug(context: str | None) -> str | None:
    offer = discount_ladder.offer_from_context(context)
    return offer.slug if offer else None


def _max_payment_platform(context: str) -> str:
    return MAX_BONUS_PLATFORM if context in {"onboarding_bonus", "onboarding_paywall"} else PLATFORM


async def _has_exactly_one_successful_max_payment(pool: asyncpg.Pool, user_id: int) -> bool:
    successful = await pool.fetchval(
        "SELECT COUNT(*) FROM yookassa_payments "
        "WHERE user_id = $1 AND platform LIKE 'max%' AND status = 'succeeded'",
        user_id,
    )
    return int(successful or 0) == 1


@router.get("/payments/price", dependencies=[AuthDep])
async def price(
    external_id: str, context: str = "onboarding_paywall",
    pool: asyncpg.Pool = Depends(get_db),
):
    """Return prices for the requested MAX checkout context.

    Menu prices and the first-purchase ladder are deliberately separate.  A
    returning customer must never be sent back to a one-off onboarding offer.
    """
    user = (await _resolve_user(pool, external_id))["user"]
    result = await _price_tiers(pool, user, context)
    result["referral_friend_bonus"] = await referrals_repo.pending_friend_bonus(pool, user_id=user["id"])
    return result


async def _price_tiers(pool: asyncpg.Pool, user: dict, context: str) -> dict:
    if context == "menu":
        return {"tiers": await pricing_repo.get_menu_tiers(pool)}
    if context.startswith(discount_ladder.CONTEXT_PREFIX):
        # An unknown, expired or foreign offer shows the ordinary packages (no offer_slug) so an old
        # push never promises a price that checkout would refuse.
        offer = discount_ladder.offer_from_context(context)
        if offer is not None and await discount_ladder.live_offer(pool, user["id"], offer.slug):
            return {"tiers": discount_ladder.tier_dicts(offer), "offer_slug": offer.slug}
        return {"tiers": await pricing_repo.get_menu_tiers(pool, bonus=False)}
    if context == "first_payment_discount":
        raise HTTPException(status_code=410, detail="Discount offers are unavailable")
    if context not in {"onboarding_trial", "onboarding_bonus", "onboarding_paywall"}:
        raise HTTPException(status_code=400, detail="Invalid payment context")
    if context == "onboarding_trial":
        if not _onboarding_trial_available(user):
            return {"tiers": await pricing_repo.get_menu_tiers(pool, bonus=False)}
        return {"tiers": await _max_onboarding_tiers(pool, context)}
    if not _onboarding_bonus_available(user):
        return {"tiers": await pricing_repo.get_menu_tiers(pool, bonus=False)}
    return {"tiers": await _max_onboarding_tiers(pool, context)}


class MaxYookassaCreateRequest(BaseModel):
    external_id: str
    generations: int
    amount_rubles: int
    context: str = "onboarding_paywall"
    operation_key: str | None = None


async def _valid_max_payment_tier(
    pool: asyncpg.Pool, user: dict, *, generations: int, amount_rubles: int, context: str,
) -> bool:
    if context == "menu":
        tiers = await pricing_repo.get_menu_tiers(pool)
        return any(
            int(tier.get("generations") or 0) == generations
            and int(tier.get("rubles") or 0) == amount_rubles
            for tier in tiers
        )
    if context == "first_payment_discount":
        return False
    if context.startswith(discount_ladder.CONTEXT_PREFIX):
        offer = discount_ladder.offer_from_context(context)
        return (
            offer is not None
            and await discount_ladder.live_offer(pool, user["id"], offer.slug) is not None
            and discount_ladder.matches(offer, generations, amount_rubles)
        )
    if context not in {"onboarding_trial", "onboarding_bonus", "onboarding_paywall"}:
        return False
    if context == "onboarding_trial":
        return (
            _onboarding_trial_available(user)
            and generations == MAX_ONBOARDING_TRIAL_TIER["generations"]
            and amount_rubles == MAX_ONBOARDING_TRIAL_TIER["rubles"]
        )
    if not _onboarding_bonus_available(user):
        return False
    return any(
        generations == int(tier["generations"]) and amount_rubles == int(tier["rubles"])
        for tier in await _max_onboarding_tiers(pool, context)
    )


# --- T-BANK --- changed: yookassa_create
@router.post("/payments/yookassa/create", dependencies=[AuthDep])
async def yookassa_create(
    body: MaxYookassaCreateRequest, pool: asyncpg.Pool = Depends(get_db),
):
    if not payment_gateway.yookassa_configured() and not tbank_acq.is_configured():
        raise HTTPException(status_code=503, detail="YooKassa not configured")

    user = (await _resolve_user(pool, body.external_id))["user"]
    provider = payment_gateway.choose_provider(user["id"])
    if not payment_gateway.configured(provider):
        raise HTTPException(status_code=503, detail="YooKassa not configured")

    if not await _valid_max_payment_tier(
        pool, user, generations=body.generations, amount_rubles=body.amount_rubles,
        context=body.context,
    ):
        raise HTTPException(status_code=400, detail="Invalid payment tier")

    # Fixed at checkout from the live menu table (never from the client); only the menu pays it.
    bonus_generations = 0
    if body.context == "menu":
        tier = next((
            tier for tier in await pricing_repo.get_menu_tiers(pool)
            if int(tier.get("generations") or 0) == body.generations
            and int(tier.get("rubles") or 0) == body.amount_rubles
        ), None)
        bonus_generations = int((tier or {}).get("bonus_generations") or 0)

    if body.operation_key:
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtext($1))",
                    f"max-payment:{user['id']}:{body.operation_key}",
                )
                payment_platform = _max_payment_platform(body.context)
                existing = await conn.fetchrow(
                    """
                    SELECT yookassa_id, status, confirmation_url, amount_rubles, generations, bonus_generations, provider
                    FROM yookassa_payments
                    WHERE user_id = $1
                      AND platform = ANY($2::varchar[])
                      AND client_operation_key = $3
                    """,
                    user["id"],
                    ([PLATFORM, MAX_BONUS_PLATFORM]
                     if body.context in {"onboarding_bonus", "onboarding_paywall"}
                     else [payment_platform]),
                    body.operation_key,
                )
                if existing and existing["status"] == "pending":
                    result = dict(existing)
                    created = False
                elif existing:
                    # A cancelled/expired confirmation URL is not reusable.
                    raise HTTPException(status_code=409, detail="Payment attempt is no longer active")
                else:
                    result = await payment_gateway.create_payment(
                        provider,
                        amount_rubles=body.amount_rubles,
                        description=f"{settings.BRAND_NAME} — пакет {body.generations} энергии",
                        generations=body.generations,
                        user_id=user["id"],
                        return_url=f"https://max.ru/{settings.MAX_BOT_USERNAME}",
                        device_data=bot_pay.BOT_DEVICE_DATA if provider == payment_gateway.TBANK and settings.TBANK_BUTTONS else None,
                    )
                    await yookassa_repo.create_payment_on_connection(
                        conn, user_id=user["id"], telegram_id=None,
                        yookassa_id=result["yookassa_id"], amount_rubles=body.amount_rubles,
                        generations=body.generations, confirmation_url=result["confirmation_url"],
                        platform=payment_platform, operation_key=body.operation_key,
                        offer_slug=_offer_slug(body.context),
                        bonus_generations=bonus_generations,
                        provider=result.get("provider", "yookassa"),
                        provider_order_id=result.get("order_id"),
                    )
                    created = True
    else:
        result = await payment_gateway.create_payment(
            provider,
            amount_rubles=body.amount_rubles,
            description=f"{settings.BRAND_NAME} — пакет {body.generations} энергии",
            generations=body.generations,
            user_id=user["id"],
            return_url=f"https://max.ru/{settings.MAX_BOT_USERNAME}",
            device_data=bot_pay.BOT_DEVICE_DATA if provider == payment_gateway.TBANK and settings.TBANK_BUTTONS else None,
        )
        await yookassa_repo.create_payment(
            pool=pool, user_id=user["id"], telegram_id=None,
            yookassa_id=result["yookassa_id"], amount_rubles=body.amount_rubles,
            generations=body.generations, confirmation_url=result["confirmation_url"],
            platform=_max_payment_platform(body.context),
            offer_slug=_offer_slug(body.context),
            bonus_generations=bonus_generations,
            provider=result.get("provider", "yookassa"),
            provider_order_id=result.get("order_id"),
        )
        created = True
    if created:
        await analytics.track(
            "yookassa_payment_created",
            user_id=_analytics_id(body.external_id),
            properties={"amount_rubles": body.amount_rubles,
                        "generations": body.generations,
                        "bonus_generations": bonus_generations,
                        "yookassa_id": result["yookassa_id"], "platform": PLATFORM,
                        "context": body.context, "offer_slug": _offer_slug(body.context),
                        "provider": result.get("provider", "yookassa")},
        )
    return {
        "yookassa_id": result["yookassa_id"],
        "confirmation_url": result["confirmation_url"],
        "generations": body.generations,
        "amount_rubles": body.amount_rubles,
        "bonus_generations": int(result.get("bonus_generations", bonus_generations) or 0),
        # T-Bank with the buttons on: see the TG create call (a repeated call of the same operation gets the same buttons).
        **await bot_pay.fields_for_bot(
            provider=result.get("provider"), payment_id=result["yookassa_id"], user_id=user["id"], platform="max",
        ),
    }
# --- /T-BANK ---


# --- T-BANK --- changed: yookassa_status
@router.get("/payments/yookassa/status", dependencies=[AuthDep])
async def yookassa_status(
    external_id: str,
    yookassa_id: str,
    request: Request,
    pool: asyncpg.Pool = Depends(get_db),
):
    user = (await _resolve_user(pool, external_id))["user"]
    local = await yookassa_repo.get_pending_by_user_id_and_yookassa_id(
        pool, user["id"], yookassa_id,
    )
    if local is None:
        existing = await yookassa_repo.get_by_yookassa_id(pool, yookassa_id)
        if existing and existing["user_id"] == user["id"]:
            return {"status": existing["status"],
                    "paid": existing["status"] == "succeeded"}
        raise HTTPException(status_code=404, detail="Payment not found")

    remote = await payment_gateway.get_payment_status(local)
    if remote.get("status") == "succeeded" and payment_gateway.matches_local_payment(
        remote, local,
    ):
        await yookassa_repo.save_payment_method(pool, yookassa_id, remote.get("payment_method"))
        settled = await yookassa_repo.settle_succeeded_payment(pool, yookassa_id)
        balance = settled["new_balance"] if settled else None
        if settled:
            await notify_referrer(notifications_client_for(request), pool, settled.get("referrer_reward"))
            await max_bot.notify_payment_succeeded(
                pool,
                user_id=settled["user_id"],
                generations=settled.get("credited_generations", settled["generations"]),
                new_balance=balance,
            )
            await analytics.track(
                "payment_completed",
                user_id=_analytics_id(external_id),
                properties={"amount": local["amount_rubles"], "currency": "RUB",
                            "method": "yookassa", "generations": local["generations"],
                            "new_balance": balance, "platform": PLATFORM},
            )
            try:
                recipient = int(external_id)
            except (TypeError, ValueError):
                recipient = None
            if recipient is not None:
                await cancel_event(
                    request, "onboarding_paywall_completed", recipient, platform=PLATFORM,
                )
                await cancel_event(
                    request, "first_payment_upsell_cancelled", recipient, platform=PLATFORM,
                )
                if settled.get("is_first_purchase"):
                    fire_event_bg(request, "first_payment_completed", recipient, {
                        "first_name": user.get("first_name") or "",
                    }, platform=PLATFORM)
        return {"status": "succeeded", "paid": True, "new_balance": balance}

    if remote.get("status") == "canceled":
        await yookassa_repo.mark_canceled(pool, yookassa_id)
        return {"status": "canceled", "paid": False}

    return {"status": local["status"], "paid": False}
# --- /T-BANK ---
