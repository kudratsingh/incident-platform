from app.models.deploy_marker import DeployMarker
from app.repositories.base import BaseRepository
from sqlalchemy import desc, func, select


class DeployMarkerRepository(BaseRepository[DeployMarker]):
    model = DeployMarker

    async def list_recent(
        self,
        limit: int = 20,
        environment: str | None = None,
    ) -> tuple[list[DeployMarker], int]:
        """Most recent deploys first. `environment` narrows to one env
        (e.g. `prod`); omit for cross-env history."""
        filters = []
        if environment is not None:
            filters.append(DeployMarker.environment == environment)

        total_stmt = select(func.count()).select_from(DeployMarker)
        rows_stmt = select(DeployMarker).order_by(desc(DeployMarker.deployed_at)).limit(limit)
        if filters:
            total_stmt = total_stmt.where(*filters)
            rows_stmt = rows_stmt.where(*filters)

        total = (await self.session.execute(total_stmt)).scalar_one()
        rows = list((await self.session.execute(rows_stmt)).scalars().all())
        return rows, int(total)
