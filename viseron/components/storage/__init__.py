"""Storage component."""
from __future__ import annotations

import logging
import os
import pathlib
from typing import TYPE_CHECKING, Any, Callable, Literal, TypedDict

import voluptuous as vol
from alembic import command, script
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, scoped_session, sessionmaker

from viseron.components.storage.config import STORAGE_SCHEMA
from viseron.components.storage.const import (
    COMPONENT,
    CONFIG_MAX_AGE,
    CONFIG_PATH,
    CONFIG_RECORDINGS,
    CONFIG_SNAPSHOTS,
    CONFIG_TIERS,
    DATABASE_URL,
    DEFAULT_COMPONENT,
    DESC_COMPONENT,
)
from viseron.components.storage.models import Base
from viseron.components.storage.tier_handler import TierHandler
from viseron.components.storage.util import (
    calculate_age,
    get_recordings_path,
    get_segments_path,
)
from viseron.const import EVENT_DOMAIN_REGISTERED, TEMP_DIR, VISERON_SIGNAL_STOPPING
from viseron.domains.camera.const import DOMAIN as CAMERA_DOMAIN
from viseron.helpers.logs import StreamToLogger

if TYPE_CHECKING:
    from viseron import Event, Viseron
    from viseron.domains.camera import AbstractCamera, FailedCamera

LOGGER = logging.getLogger(__name__)


def validate_tiers(config: dict[str, Any]) -> dict[str, Any]:
    """Validate tiers.

    Paths cannot be reserved paths.
    The same path cannot be defined multiple times.
    max_age has to be greater than previous tier max_age.
    """
    component_config = config[COMPONENT]

    previous_tier: None | dict[str, Any] = None
    paths: list[str] = []
    for tier in component_config[CONFIG_RECORDINGS][CONFIG_TIERS]:
        if tier[CONFIG_PATH] in ["/tmp", TEMP_DIR]:
            raise vol.Invalid(
                f"Tier {tier[CONFIG_PATH]} is a reserved path and cannot be used"
            )

        if tier[CONFIG_PATH] in paths:
            raise vol.Invalid(f"Tier {tier[CONFIG_PATH]} is defined multiple times")
        paths.append(tier[CONFIG_PATH])

        if previous_tier is None:
            previous_tier = tier
            continue

        tier_max_age = calculate_age(tier[CONFIG_MAX_AGE]).total_seconds()
        previous_tier_max_age = calculate_age(
            previous_tier[CONFIG_MAX_AGE]
        ).total_seconds()

        if (
            tier_max_age > 0  # pylint: disable=chained-comparison
            and tier_max_age <= previous_tier_max_age
        ):
            raise vol.Invalid(
                f"Tier {tier[CONFIG_PATH]} "
                "max_age must be greater than previous tier max_age"
            )
        previous_tier = tier
    return config


CONFIG_SCHEMA = vol.Schema(
    vol.All(
        {
            vol.Required(
                COMPONENT, default=DEFAULT_COMPONENT, description=DESC_COMPONENT
            ): STORAGE_SCHEMA
        },
        validate_tiers,
    ),
    extra=vol.ALLOW_EXTRA,
)


class TierCategories(TypedDict):
    """Tier categories."""

    recordings: list[Literal["segments", "recordings"]]
    snapshots: list[Literal["face_recognition", "object_detection"]]


TIER_CATEGORIES: TierCategories = {
    "recordings": [
        "segments",
        "recordings",
    ],
    "snapshots": [
        "face_recognition",
        "object_detection",
    ],
}


def setup(vis: Viseron, config: dict[str, Any]) -> bool:
    """Set up storage component."""
    vis.data[COMPONENT] = Storage(vis, config[COMPONENT])
    return True


class Storage:
    """Storage component.

    It handles the database connection as well as file storage.

    This component will move stored items up tiers when they reach the max age or max
    size.
    """

    def __init__(self, vis: Viseron, config: dict[str, Any]) -> None:
        self._vis = vis
        self._config = config
        self._recordings_tiers = config[CONFIG_RECORDINGS][CONFIG_TIERS]
        self._snapshots_tiers = config[CONFIG_SNAPSHOTS][CONFIG_TIERS]

        self.engine: Engine | None = None
        self._get_session: Callable[[], Session] | None = None
        self._alembic_cfg = self._get_alembic_config()
        self.create_database()

        vis.listen_event(
            EVENT_DOMAIN_REGISTERED.format(domain=CAMERA_DOMAIN),
            self._camera_registered,
        )
        vis.register_signal_handler(VISERON_SIGNAL_STOPPING, self._shutdown)

    def _get_alembic_config(self) -> Config:
        base_path = pathlib.Path(__file__).parent.resolve()
        alembic_cfg = Config(
            os.path.join(base_path, "alembic.ini"),
            stdout=StreamToLogger(
                logging.getLogger("alembic.stdout"),
                logging.INFO,
            ),
        )
        alembic_cfg.set_main_option(
            "script_location", os.path.join(base_path, "alembic")
        )
        return alembic_cfg

    def _run_migrations(self) -> None:
        """Run database migrations.

        Checks to see if there are any upgrades to be done and applies them.
        """
        LOGGER.warning("Upgrading database, DO NOT INTERRUPT")
        command.upgrade(self._alembic_cfg, "head")
        LOGGER.warning("Database upgrade complete")

    def _create_new_db(self) -> None:
        """Create and stamp a new DB for fresh installs."""
        LOGGER.debug("Creating new database")
        if self.engine is None:
            raise RuntimeError("The database connection has not been established")

        try:
            Base.metadata.create_all(self.engine)
            command.stamp(self._alembic_cfg, "head")
        except Exception as error:  # pylint: disable=[broad-exception-caught]
            LOGGER.error(f"Failed to create new database: {error}", exc_info=True)

    def create_database(self) -> None:
        """Create database."""
        self.engine = create_engine(DATABASE_URL)

        conn = self.engine.connect()
        context = MigrationContext.configure(conn)
        current_rev = context.get_current_revision()
        LOGGER.debug(f"Current database revision: {current_rev}")

        _script = script.ScriptDirectory.from_config(self._alembic_cfg)

        if current_rev is None:
            self._create_new_db()
        elif current_rev != _script.get_current_head():
            self._run_migrations()

        self._get_session = scoped_session(sessionmaker(bind=self.engine, future=True))

    def get_session(self) -> Session:
        """Get a new sqlalchemy session."""
        if self._get_session is None:
            raise RuntimeError("The database connection has not been established")
        return self._get_session()

    def get_recordings_path(self, camera: AbstractCamera | FailedCamera) -> str:
        """Get recordings path for camera."""
        return get_recordings_path(self._recordings_tiers[0], camera)

    def get_segments_path(self, camera: AbstractCamera | FailedCamera) -> str:
        """Get segments path for camera."""
        return get_segments_path(self._recordings_tiers[0], camera)

    def _camera_registered(self, event_data: Event[AbstractCamera]) -> None:
        """Start observer for camera."""
        camera = event_data.data

        for category in TIER_CATEGORIES:
            tiers = self._config[category][CONFIG_TIERS]
            for index, tier in enumerate(tiers):
                if index == len(tiers) - 1:
                    next_tier = None
                else:
                    next_tier = tiers[index + 1]
                # pylint: disable-next=line-too-long
                for subcategory in TIER_CATEGORIES[category]:  # type: ignore[literal-required]
                    TierHandler(
                        self._vis,
                        camera,
                        index,
                        category,
                        subcategory,
                        tier,
                        next_tier,
                    )

    def _shutdown(self) -> None:
        """Shutdown."""
        if self.engine:
            self.engine.dispose()
