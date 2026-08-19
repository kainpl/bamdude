"""Sub-projects: a master project's figures should cover its whole tree.

Ported from upstream #1264, adapted. The `parent_id` column and the child list
existed on both sides; what was missing is everything that makes nesting mean
something — a roll-up, a guard that keeps the tree a tree, and somewhere for the
children to go when a middle project is deleted.

⚠️ The roll-up is a SECOND figure, not a widening of the project's own stats.
Nesting has been settable over the API all along, so broadening the existing
numbers would silently restate the history of anyone who already used it.
"""

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from backend.app.api.routes.projects import delete_project
from backend.app.models.archive import PrintArchive
from backend.app.models.project import Project


async def _project(db_session, name: str, *, parent_id: int | None = None, **fields) -> Project:
    project = Project(name=name, parent_id=parent_id, **fields)
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    return project


async def _print(db_session, project: Project, *, grams: float, cost: float, status: str = "completed"):
    db_session.add(
        PrintArchive(
            filename=f"{project.name}.3mf",
            print_name=project.name,
            file_path=f"/tmp/{project.name}",
            file_size=1,
            content_hash=f"h_{project.name}_{grams}_{cost}",
            status=status,
            project_id=project.id,
            quantity=1,
            filament_used_grams=grams,
            cost=cost,
        )
    )
    await db_session.commit()


@pytest.mark.asyncio
@pytest.mark.integration
class TestTheRollUp:
    async def test_a_master_project_adds_up_its_whole_tree(self, async_client: AsyncClient, db_session):
        master = await _project(db_session, "master")
        child = await _project(db_session, "child", parent_id=master.id)
        grandchild = await _project(db_session, "grandchild", parent_id=child.id)
        await _print(db_session, master, grams=10.0, cost=1.0)
        await _print(db_session, child, grams=20.0, cost=2.0)
        await _print(db_session, grandchild, grams=30.0, cost=3.0)

        body = (await async_client.get(f"/api/v1/projects/{master.id}")).json()

        assert body["stats"]["total_filament_grams"] == 10.0, "its own card keeps its own meaning"
        assert body["rollup_stats"]["total_filament_grams"] == 60.0
        assert body["rollup_stats"]["estimated_cost"] == 6.0
        assert body["rollup_stats"]["total_archives"] == 3

    async def test_a_project_with_no_children_gets_no_second_card(self, async_client: AsyncClient, db_session):
        """A duplicate card saying the same numbers reads as a bug."""
        lonely = await _project(db_session, "lonely")
        await _print(db_session, lonely, grams=5.0, cost=1.0)

        body = (await async_client.get(f"/api/v1/projects/{lonely.id}")).json()

        assert body["rollup_stats"] is None
        assert body["stats"]["total_filament_grams"] == 5.0

    async def test_targets_are_added_up_too(self, async_client: AsyncClient, db_session):
        """Progress for a tree is measured against what the tree set out to do."""
        master = await _project(db_session, "master", target_count=2)
        child = await _project(db_session, "child", parent_id=master.id, target_count=8)
        await _print(db_session, master, grams=1.0, cost=1.0)
        await _print(db_session, child, grams=1.0, cost=1.0)

        body = (await async_client.get(f"/api/v1/projects/{master.id}")).json()

        assert body["stats"]["progress_percent"] == 50.0, "1 of its own 2"
        assert body["rollup_stats"]["progress_percent"] == 20.0, "2 of the tree's 10"


@pytest.mark.asyncio
@pytest.mark.integration
class TestTheTreeStaysATree:
    async def test_a_project_cannot_be_its_own_parent(self, async_client: AsyncClient, db_session):
        project = await _project(db_session, "self")

        response = await async_client.patch(f"/api/v1/projects/{project.id}", json={"parent_id": project.id})

        assert response.status_code == 400

    async def test_a_two_step_loop_is_refused(self, async_client: AsyncClient, db_session):
        """⚠️ The case the self-parent check alone missed: B is already under A,
        and the second call tries to put A under B. The result would be a tree
        with no root to roll figures up to."""
        a = await _project(db_session, "a")
        await _project(db_session, "b", parent_id=a.id)
        b_id = (await _project(db_session, "b2", parent_id=a.id)).id

        response = await async_client.patch(f"/api/v1/projects/{a.id}", json={"parent_id": b_id})

        assert response.status_code == 400

    async def test_a_three_step_loop_is_refused(self, async_client: AsyncClient, db_session):
        a = await _project(db_session, "a")
        b = await _project(db_session, "b", parent_id=a.id)
        c = await _project(db_session, "c", parent_id=b.id)

        response = await async_client.patch(f"/api/v1/projects/{a.id}", json={"parent_id": c.id})

        assert response.status_code == 400

    async def test_a_legitimate_reparent_still_works(self, async_client: AsyncClient, db_session):
        """The guard must not refuse the ordinary case."""
        one = await _project(db_session, "one")
        two = await _project(db_session, "two")

        response = await async_client.patch(f"/api/v1/projects/{two.id}", json={"parent_id": one.id})

        assert response.status_code == 200
        assert response.json()["parent_id"] == one.id

    async def test_detaching_still_works(self, async_client: AsyncClient, db_session):
        parent = await _project(db_session, "parent")
        child = await _project(db_session, "child", parent_id=parent.id)

        response = await async_client.patch(f"/api/v1/projects/{child.id}", json={"parent_id": 0})

        assert response.status_code == 200
        assert response.json()["parent_id"] is None


@pytest.mark.asyncio
@pytest.mark.integration
class TestDeletingFromTheMiddle:
    """⚠️ These call the route function directly rather than going through
    ``async_client``.

    The test harness's ``get_db`` override yields a session and never commits,
    while the real dependency commits after the response — so a route that
    leaves the commit to the dependency (this one does) writes nothing an HTTP
    test can observe. Driving the function and committing here tests the same
    code against a session whose writes actually land.
    """

    async def _parent_of(self, db_session, project_id: int) -> int | None:
        row = await db_session.execute(select(Project.parent_id).where(Project.id == project_id))
        return row.scalar_one()

    async def test_children_move_up_rather_than_out(self, db_session):
        """⚠️ Not to the top level: a grandchild belongs to the tree, and
        nulling its parent silently drops it out of the grouping that nesting
        exists to provide."""
        master = await _project(db_session, "master")
        middle = await _project(db_session, "middle", parent_id=master.id)
        leaf = await _project(db_session, "leaf", parent_id=middle.id)
        master_id, leaf_id = master.id, leaf.id

        await delete_project(project_id=middle.id, db=db_session, _=None)
        await db_session.commit()

        assert await self._parent_of(db_session, leaf_id) == master_id

    async def test_deleting_a_root_leaves_its_children_at_the_top(self, db_session):
        """There is nowhere further up to go, and that is the correct answer."""
        root = await _project(db_session, "root")
        child = await _project(db_session, "child", parent_id=root.id)
        child_id = child.id

        await delete_project(project_id=root.id, db=db_session, _=None)
        await db_session.commit()

        assert await self._parent_of(db_session, child_id) is None

    async def test_the_project_itself_is_gone(self, db_session):
        parent = await _project(db_session, "parent")
        child = await _project(db_session, "child", parent_id=parent.id)
        child_id = child.id

        await delete_project(project_id=parent.id, db=db_session, _=None)
        await db_session.commit()

        remaining = await db_session.execute(select(Project.id))
        assert remaining.scalars().all() == [child_id]


@pytest.mark.asyncio
@pytest.mark.integration
class TestTheWalkSurvivesABadTree:
    async def test_a_pre_existing_cycle_does_not_hang_the_roll_up(self, async_client: AsyncClient, db_session):
        """A database written before the guard existed can already hold a loop.
        A roll-up that hangs is worse than one that is wrong."""
        a = await _project(db_session, "a")
        b = await _project(db_session, "b", parent_id=a.id)
        a.parent_id = b.id  # straight into the table, as an old row would be
        await db_session.commit()

        response = await async_client.get(f"/api/v1/projects/{a.id}")

        assert response.status_code == 200
