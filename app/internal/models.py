import json
import uuid
from datetime import datetime
from enum import Enum
from typing import Annotated, Literal, Union, cast

from pydantic import BaseModel, ConfigDict
from sqlmodel import JSON, Column, DateTime, Field, SQLModel, UniqueConstraint, func
from sqlmodel._compat import SQLModelConfig
from sqlmodel.main import Relationship


class BaseSQLModel(SQLModel):
    pass


class GroupEnum(str, Enum):
    untrusted = "untrusted"
    trusted = "trusted"
    admin = "admin"


class DownloadStatusEnum(str, Enum):
    """How far a book has actually got.

    `Audiobook.downloaded` stays the "this has been actioned, do not grab or
    recommend it again" guard it has always been. This tracks what really
    happened, which Prowlarr never tells us directly.
    """

    grabbed = "grabbed"
    """Handed to Prowlarr. The files may or may not ever arrive."""
    downloaded = "downloaded"
    """Files confirmed on disk by the library watcher."""
    failed = "failed"
    """The grab never produced files, or organizing them failed."""


class User(BaseSQLModel, table=True):
    username: str = Field(primary_key=True)
    password: str
    group: GroupEnum = Field(
        default=GroupEnum.untrusted,
        sa_column_kwargs={"server_default": "untrusted"},
    )
    root: bool = False
    extra_data: str | None = None
    request_limit: int | None = None
    """Requests allowed per period. None uses the limit set for the user's group, 0 means unlimited."""
    abs_user_id: str | None = None
    """Set for accounts that sign in with their Audiobookshelf login."""

    # TODO: Add last_login
    # last_login: datetime = Field(
    #     default_factory=datetime.now, sa_column_kwargs={"server_default": "now()"}
    # )

    """
    untrusted: Requests need to be manually reviewed
    trusted: Requests are automatically downloaded if possible
    admin: Can approve or deny requests, change settings, etc.
    """

    def is_above(self, group: GroupEnum) -> bool:
        if group == "admin":
            if self.group != GroupEnum.admin:
                return False
        elif group == "trusted":
            if self.group not in [GroupEnum.admin, GroupEnum.trusted]:
                return False
        return True

    def can_download(self):
        return self.is_above(GroupEnum.trusted)

    def is_admin(self):
        return self.group == GroupEnum.admin

    def is_self(self, username: str):
        # To prevent '==' in Jinja2, since that breaks formatting
        return self.username == username


class Audiobook(BaseSQLModel, table=True):
    """A cached Audible audiobook result. Used for both the search results and also linked to via a foreign key for requests."""

    asin: str = Field(primary_key=True)
    title: str
    subtitle: str | None
    authors: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    narrators: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    cover_image: str | None
    release_date: datetime
    runtime_length_min: int
    series: str | None = None
    series_position: str | None = None
    language: str | None = None
    """As Audible names it, e.g. "english". Unknown for books cached before it was stored."""
    quality_format: str | None = None
    """Format of the release last grabbed (flac, m4b, mp3, ...), to know when an upgrade is worth it."""
    download_status: DownloadStatusEnum | None = Field(default=None, index=True)
    search_attempts: int = Field(default=0)
    last_searched_at: datetime | None = Field(default=None)
    updated_at: datetime = Field(
        default_factory=datetime.now,
        sa_column=Column(
            # Python's clock, like every other timestamp the app writes. The
            # database's now() is UTC on SQLite, which put updates hours off
            # from creation times stored in local time.
            onupdate=datetime.now,
            server_default=func.now(),
            type_=DateTime,
            nullable=False,
        ),
    )
    downloaded: bool = False

    requests: list["AudiobookRequest"] = Relationship(back_populates="audiobook")  # pyright: ignore[reportAny]

    model_config: SQLModelConfig = cast(
        SQLModelConfig, cast(object, ConfigDict(arbitrary_types_allowed=True))
    )

    @property
    def runtime_length_hrs(self):
        return round(self.runtime_length_min / 60, 1)

    @property
    def is_released(self) -> bool:
        return self.release_date <= datetime.now()


class AudiobookWithRequests(BaseModel):
    book: Audiobook
    requests: list[AudiobookRequest]
    username: str | None

    @property
    def already_requested(self):
        if self.username:
            return any(req.user_username == self.username for req in self.requests)
        return len(self.requests) > 0


class AudiobookRequest(BaseSQLModel, table=True):
    asin: str = Field(
        primary_key=True,
        foreign_key="audiobook.asin",
        ondelete="CASCADE",
    )
    user_username: str = Field(
        primary_key=True,
        foreign_key="user.username",
        ondelete="CASCADE",
    )
    updated_at: datetime = Field(
        default_factory=datetime.now,
        sa_column=Column(
            # Python's clock, like every other timestamp the app writes. The
            # database's now() is UTC on SQLite, which put updates hours off
            # from creation times stored in local time.
            onupdate=datetime.now,
            server_default=func.now(),
            type_=DateTime,
            nullable=False,
        ),
    )
    approved: bool = False
    """An admin said yes, so it is treated like a trusted user's request."""
    created_at: datetime = Field(
        default_factory=datetime.now,
        sa_column=Column(
            server_default=func.now(),
            type_=DateTime,
            nullable=False,
        ),
    )
    via_follow: str | None = None
    """The followed author or series that requested this automatically."""

    audiobook: Audiobook = Relationship(back_populates="requests")  # pyright: ignore[reportAny]
    user: User = Relationship()  # pyright: ignore[reportAny]

    model_config: SQLModelConfig = cast(
        SQLModelConfig, cast(object, ConfigDict(arbitrary_types_allowed=True))
    )


class AudiobookWishlistResult(BaseModel):
    book: Audiobook
    requests: list[AudiobookRequest]
    download_error: str | None = None

    @property
    def amount_requested(self):
        return len(self.requests)

    @property
    def requested_by_usernames(self):
        return "\n".join(req.user_username for req in self.requests)

    @property
    def awaiting_approval(self) -> bool:
        """Only untrusted users asked for it, and no admin has approved it yet.

        Once a download has been tried, someone has already acted on it.
        """
        if self.book.downloaded or self.book.download_status or not self.requests:
            return False
        return not any(
            req.approved or req.user.is_above(GroupEnum.trusted)
            for req in self.requests
        )


class ManualBookRequest(BaseSQLModel, table=True):
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    user_username: str = Field(foreign_key="user.username", ondelete="CASCADE")
    title: str
    subtitle: str | None = None
    authors: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    narrators: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    publish_date: str | None = None
    additional_info: str | None = None
    download_status: DownloadStatusEnum | None = Field(default=None, index=True)
    search_attempts: int = Field(default=0)
    last_searched_at: datetime | None = Field(default=None)
    updated_at: datetime = Field(
        default_factory=datetime.now,
        sa_column=Column(
            # Python's clock, like every other timestamp the app writes. The
            # database's now() is UTC on SQLite, which put updates hours off
            # from creation times stored in local time.
            onupdate=datetime.now,
            server_default=func.now(),
            type_=DateTime,
            nullable=False,
        ),
    )
    downloaded: bool = False
    approved: bool = False
    """An admin said yes, so it is treated like a trusted user's request."""
    created_at: datetime = Field(
        default_factory=datetime.now,
        sa_column=Column(
            server_default=func.now(),
            type_=DateTime,
            nullable=False,
        ),
    )

    user: User | None = Relationship()  # pyright: ignore[reportAny]

    model_config: SQLModelConfig = cast(
        SQLModelConfig, cast(object, ConfigDict(arbitrary_types_allowed=True))
    )

    @property
    def awaiting_approval(self) -> bool:
        """Asked for by an untrusted user, and no admin has approved it yet."""
        if self.downloaded or self.approved or self.download_status:
            return False
        return self.user is None or not self.user.is_above(GroupEnum.trusted)


class BookMetadata(BaseSQLModel):
    """extra metadata that can be added to sources to better rank them"""

    title: str | None = None
    subtitle: str | None = None
    authors: list[str] = []
    narrators: list[str] = []
    filetype: str | None = None


class BaseSource(BaseSQLModel):
    guid: str
    indexer_id: int
    indexer: str
    title: str
    size: int  # in bytes
    publish_date: datetime
    info_url: str | None
    indexer_flags: list[str]
    download_url: str | None = None
    magnet_url: str | None = None

    book_metadata: BookMetadata = BookMetadata()

    @property
    def size_MB(self):
        return round(self.size / 1e6, 1)


class TorrentSource(BaseSource):
    protocol: Literal["torrent"] = "torrent"
    seeders: int
    leechers: int


class UsenetSource(BaseSource):
    protocol: Literal["usenet"] = "usenet"
    grabs: int


ProwlarrSource = Annotated[
    Union[TorrentSource, UsenetSource], Field(discriminator="protocol")
]


class Indexer(BaseModel, frozen=True):
    id: int
    name: str
    enable: bool
    privacy: str


class Config(BaseSQLModel, table=True):
    key: str = Field(primary_key=True)
    value: str


class EventEnum(str, Enum):
    on_new_request = "onNewRequest"
    """Someone requested a book."""
    on_grabbed = "onGrabbed"
    """A source was sent to the download client."""
    on_successful_download = "onSuccessfulDownload"
    """The files are in the library. With the library organizer on, this waits until they have been placed; otherwise it fires right after the grab. Also sent when an admin marks a book as downloaded."""
    on_failed_download = "onFailedDownload"
    """Prowlarr could not start the download."""
    on_request_approved = "onRequestApproved"
    """An admin approved a request from someone whose requests need approval."""
    on_request_declined = "onRequestDeclined"
    """An admin declined a request. `{declineReason}` holds the reason they gave."""
    on_issue_reported = "onIssueReported"
    """Someone reported a problem with a book. `{issueKind}` and `{issueMessage}` say what."""
    on_issue_resolved = "onIssueResolved"
    """An admin resolved a reported problem. `{issueResolution}` says how."""


class NotificationBodyTypeEnum(str, Enum):
    text = "text"
    json = "json"


class Notification(BaseSQLModel, table=True):
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    name: str
    url: str
    headers: dict[str, str] = Field(default_factory=dict, sa_column=Column(JSON))
    event: EventEnum
    body_type: NotificationBodyTypeEnum
    body: str
    enabled: bool

    @property
    def serialized_headers(self):
        return json.dumps(self.headers)


class OrganizeModeEnum(str, Enum):
    """How a finished download is placed into the library."""

    hardlink = "hardlink"
    copy = "copy"
    move = "move"


class LibraryImportStatusEnum(str, Enum):
    pending = "pending"
    imported = "imported"
    failed = "failed"
    expired = "expired"


class LibraryImport(BaseSQLModel, table=True):
    """A finished grab that should be placed into the library folder structure.

    A row is created whenever a download is handed to Prowlarr. The library
    watcher later matches `release_title` against the directories inside the
    completed downloads folder to find the actual files.
    """

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    asin_or_uuid: str
    """ASIN of an Audiobook or the UUID of a ManualBookRequest."""
    book_title: str
    release_title: str
    """Title of the grabbed source. Download clients usually name the folder after it."""
    client_id: str | None = None
    """Torrent info hash, so the download client can be asked about it directly."""
    protocol: str | None = None
    """torrent or usenet, to decide which client to ask."""
    guid: str | None = None
    """Prowlarr's id for the release, so a bad one can be blocklisted exactly."""
    indexer: str | None = None
    replaces: bool = False
    """An upgrade or replacement: the book's old audio files go once this one is in."""
    status: LibraryImportStatusEnum = Field(
        default=LibraryImportStatusEnum.pending,
        index=True,
        sa_column_kwargs={"server_default": "pending"},
    )
    source_path: str | None = None
    target_path: str | None = None
    error: str | None = None
    created_at: datetime = Field(
        default_factory=datetime.now,
        sa_column=Column(
            server_default=func.now(),
            type_=DateTime,
            nullable=False,
        ),
    )
    updated_at: datetime = Field(
        default_factory=datetime.now,
        sa_column=Column(
            # Python's clock, like every other timestamp the app writes. The
            # database's now() is UTC on SQLite, which put updates hours off
            # from creation times stored in local time.
            onupdate=datetime.now,
            server_default=func.now(),
            type_=DateTime,
            nullable=False,
        ),
    )


class FollowKindEnum(str, Enum):
    author = "author"
    series = "series"


class Follow(BaseSQLModel, table=True):
    """An author or series someone wants every new book from."""

    __table_args__ = (  # pyright: ignore[reportUnannotatedClassAttribute]
        UniqueConstraint("user_username", "kind", "audible_id"),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    user_username: str = Field(
        foreign_key="user.username", ondelete="CASCADE", index=True
    )
    kind: FollowKindEnum
    audible_id: str
    """The author's or series' ASIN. An author Audible has no ASIN for is keyed by name."""
    name: str
    known_asins: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    """Books already seen, so only new ones get requested."""
    last_checked_at: datetime | None = None
    last_error: str | None = None
    created_at: datetime = Field(
        default_factory=datetime.now,
        sa_column=Column(
            server_default=func.now(),
            type_=DateTime,
            nullable=False,
        ),
    )

    model_config: SQLModelConfig = cast(
        SQLModelConfig, cast(object, ConfigDict(arbitrary_types_allowed=True))
    )


class BlocklistedRelease(BaseSQLModel, table=True):
    """A release that must not be grabbed again, because it failed or was bad."""

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    title: str = Field(index=True)
    guid: str | None = Field(default=None, index=True)
    indexer: str | None = None
    asin_or_uuid: str | None = None
    """The book it was grabbed for."""
    book_title: str | None = None
    reason: str | None = None
    created_at: datetime = Field(
        default_factory=datetime.now,
        sa_column=Column(
            server_default=func.now(),
            type_=DateTime,
            nullable=False,
        ),
    )


class IssueKindEnum(str, Enum):
    wrong_book = "wrong_book"
    bad_audio = "bad_audio"
    incomplete = "incomplete"
    other = "other"

    @property
    def label(self) -> str:
        return {
            "wrong_book": "Wrong book",
            "bad_audio": "Bad audio",
            "incomplete": "Missing parts",
            "other": "Something else",
        }[self.value]


class IssueStatusEnum(str, Enum):
    open = "open"
    resolved = "resolved"


class Issue(BaseSQLModel, table=True):
    """A problem someone reported with a book that reached the library."""

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    asin: str = Field(index=True)
    book_title: str
    user_username: str = Field(foreign_key="user.username", ondelete="CASCADE")
    kind: IssueKindEnum
    message: str | None = None
    status: IssueStatusEnum = Field(
        default=IssueStatusEnum.open,
        index=True,
        sa_column_kwargs={"server_default": "open"},
    )
    resolution: str | None = None
    resolved_by: str | None = None
    created_at: datetime = Field(
        default_factory=datetime.now,
        sa_column=Column(
            server_default=func.now(),
            type_=DateTime,
            nullable=False,
        ),
    )
    resolved_at: datetime | None = None


class APIKeyResponse(BaseModel):
    id: uuid.UUID
    name: str
    enabled: bool


class APIKey(BaseSQLModel, table=True):
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    user_username: str = Field(foreign_key="user.username", ondelete="CASCADE")
    name: str
    key_hash: str
    created_at: datetime = Field(
        default_factory=datetime.now,
        sa_column=Column(
            server_default=func.now(),
            type_=DateTime,
            nullable=False,
        ),
    )
    last_used: datetime | None = Field(
        default=None,
        sa_column=Column(
            type_=DateTime,
            nullable=True,
        ),
    )
    enabled: bool = True
