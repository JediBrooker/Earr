from typing import Annotated

from fastapi import APIRouter, Depends, Response, Security
from pydantic import BaseModel
from sqlmodel import Session

from app.internal.auth.authentication import AnyAuth, DetailedUser
from app.internal.models import GroupEnum
from app.internal.ranking.quality import IndexerFlag, QualityRange, quality_config
from app.internal.research_scheduler import reschedule
from app.util.db import get_session

router = APIRouter(prefix="/download")


class DownloadSettings(BaseModel):
    auto_download: bool
    research_enabled: bool
    research_interval: int
    research_max_attempts: int
    flac_range: QualityRange
    m4b_range: QualityRange
    mp3_range: QualityRange
    unknown_audio_range: QualityRange
    unknown_range: QualityRange
    min_seeders: int
    name_ratio: int
    title_ratio: int
    indexer_flags: list[IndexerFlag]


@router.get("", response_model=DownloadSettings)
def get_download_settings(
    session: Annotated[Session, Depends(get_session)],
    _: Annotated[DetailedUser, Security(AnyAuth(GroupEnum.admin))],
):
    return DownloadSettings(
        auto_download=quality_config.get_auto_download(session),
        research_enabled=quality_config.get_research_enabled(session),
        research_interval=quality_config.get_research_interval(session),
        research_max_attempts=quality_config.get_research_max_attempts(session),
        flac_range=quality_config.get_range(session, "quality_flac"),
        m4b_range=quality_config.get_range(session, "quality_m4b"),
        mp3_range=quality_config.get_range(session, "quality_mp3"),
        unknown_audio_range=quality_config.get_range(session, "quality_unknown_audio"),
        unknown_range=quality_config.get_range(session, "quality_unknown"),
        min_seeders=quality_config.get_min_seeders(session),
        name_ratio=quality_config.get_name_exists_ratio(session),
        title_ratio=quality_config.get_title_exists_ratio(session),
        indexer_flags=quality_config.get_indexer_flags(session),
    )


class UpdateDownloadSettings(BaseModel):
    auto_download: bool
    research_enabled: bool = False
    research_interval: int = 6 * 60 * 60
    research_max_attempts: int = 5
    flac_range: QualityRange
    m4b_range: QualityRange
    mp3_range: QualityRange
    unknown_audio_range: QualityRange
    unknown_range: QualityRange
    min_seeders: int
    name_ratio: int
    title_ratio: int


@router.patch("", status_code=204)
def update_download_settings(
    body: UpdateDownloadSettings,
    session: Annotated[Session, Depends(get_session)],
    _: Annotated[DetailedUser, Security(AnyAuth(GroupEnum.admin))],
):
    quality_config.set_auto_download(session, body.auto_download)
    quality_config.set_research_enabled(session, body.research_enabled)
    quality_config.set_research_interval(session, body.research_interval)
    quality_config.set_research_max_attempts(session, body.research_max_attempts)
    reschedule(quality_config.get_research_interval(session))
    quality_config.set_range(session, "quality_flac", body.flac_range)
    quality_config.set_range(session, "quality_m4b", body.m4b_range)
    quality_config.set_range(session, "quality_mp3", body.mp3_range)
    quality_config.set_range(session, "quality_unknown_audio", body.unknown_audio_range)
    quality_config.set_range(session, "quality_unknown", body.unknown_range)
    quality_config.set_min_seeders(session, body.min_seeders)
    quality_config.set_name_exists_ratio(session, body.name_ratio)
    quality_config.set_title_exists_ratio(session, body.title_ratio)

    return Response(status_code=204)
