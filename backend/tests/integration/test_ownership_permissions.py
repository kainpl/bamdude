"""Integration tests for ownership-based permission system.

Tests the ownership permission model where users can have:
- *_all permissions: can modify any item
- *_own permissions: can only modify items they created
- Ownerless items (created_by_id = null) require *_all permission
"""

import pytest
from httpx import AsyncClient


class TestOwnershipPermissionsSetup:
    """Helper fixture class for ownership permission tests."""

    @pytest.fixture
    async def auth_setup(self, async_client: AsyncClient):
        """Create test users with different permission levels using the pre-seeded admin."""
        from backend.app.core.auth import create_access_token

        admin_token = create_access_token(data={"sub": "test_admin"})
        # /auth/me returns the admin record we need for the return payload.
        admin_user = (await async_client.get("/api/v1/auth/me")).json()

        # Get group IDs
        groups_response = await async_client.get(
            "/api/v1/groups/",
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        groups = groups_response.json()
        operators_group = next(g for g in groups if g["name"] == "Operators")
        viewers_group = next(g for g in groups if g["name"] == "Viewers")

        # Create operator user (has *_own permissions)
        operator_response = await async_client.post(
            "/api/v1/users/",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={
                "username": "operator1",
                "password": "OperatorPass123!",
                "group_ids": [operators_group["id"]],
            },
        )
        operator_user = operator_response.json()

        # Login as operator
        operator_login = await async_client.post(
            "/api/v1/auth/login",
            json={"username": "operator1", "password": "OperatorPass123!"},
        )
        operator_token = operator_login.json()["access_token"]

        # Create second operator (for cross-user tests)
        operator2_response = await async_client.post(
            "/api/v1/users/",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={
                "username": "operator2",
                "password": "OperatorPass123!",
                "group_ids": [operators_group["id"]],
            },
        )
        operator2_user = operator2_response.json()

        operator2_login = await async_client.post(
            "/api/v1/auth/login",
            json={"username": "operator2", "password": "OperatorPass123!"},
        )
        operator2_token = operator2_login.json()["access_token"]

        # Create viewer user (has no update/delete permissions)
        await async_client.post(
            "/api/v1/users/",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={
                "username": "viewer1",
                "password": "ViewerPass123!",
                "group_ids": [viewers_group["id"]],
            },
        )

        viewer_login = await async_client.post(
            "/api/v1/auth/login",
            json={"username": "viewer1", "password": "ViewerPass123!"},
        )
        viewer_token = viewer_login.json()["access_token"]

        return {
            "admin_token": admin_token,
            "admin_user": admin_user,
            "operator_token": operator_token,
            "operator_user": operator_user,
            "operator2_token": operator2_token,
            "operator2_user": operator2_user,
            "viewer_token": viewer_token,
        }


class TestArchiveOwnershipPermissions(TestOwnershipPermissionsSetup):
    """Tests for archive ownership-based permissions."""

    # ========================================================================
    # DELETE permissions
    # ========================================================================

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_admin_can_delete_any_archive(
        self, async_client: AsyncClient, auth_setup, archive_factory, printer_factory, db_session
    ):
        """Admin with *_all permissions can delete any archive."""
        printer = await printer_factory()
        # Create archive owned by operator
        archive = await archive_factory(
            printer.id,
            print_name="Operator Archive",
            created_by_id=auth_setup["operator_user"]["id"],
        )

        # Admin deletes it
        response = await async_client.delete(
            f"/api/v1/archives/{archive.id}",
            headers={"Authorization": f"Bearer {auth_setup['admin_token']}"},
        )

        assert response.status_code == 200

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_operator_can_delete_own_archive(
        self, async_client: AsyncClient, auth_setup, archive_factory, printer_factory, db_session
    ):
        """Operator with *_own permissions can delete their own archive."""
        printer = await printer_factory()
        archive = await archive_factory(
            printer.id,
            print_name="My Archive",
            created_by_id=auth_setup["operator_user"]["id"],
        )

        response = await async_client.delete(
            f"/api/v1/archives/{archive.id}",
            headers={"Authorization": f"Bearer {auth_setup['operator_token']}"},
        )

        assert response.status_code == 200

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_operator_cannot_delete_others_archive(
        self, async_client: AsyncClient, auth_setup, archive_factory, printer_factory, db_session
    ):
        """Operator with *_own permissions cannot delete another user's archive."""
        printer = await printer_factory()
        # Archive created by operator2
        archive = await archive_factory(
            printer.id,
            print_name="Other's Archive",
            created_by_id=auth_setup["operator2_user"]["id"],
        )

        # operator1 tries to delete it
        response = await async_client.delete(
            f"/api/v1/archives/{archive.id}",
            headers={"Authorization": f"Bearer {auth_setup['operator_token']}"},
        )

        assert response.status_code == 403
        assert "your own" in response.json()["detail"].lower()

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_operator_cannot_delete_ownerless_archive(
        self, async_client: AsyncClient, auth_setup, archive_factory, printer_factory, db_session
    ):
        """Operator with *_own permissions cannot delete ownerless archive."""
        printer = await printer_factory()
        # Archive with no owner (legacy data)
        archive = await archive_factory(
            printer.id,
            print_name="Ownerless Archive",
            created_by_id=None,
        )

        response = await async_client.delete(
            f"/api/v1/archives/{archive.id}",
            headers={"Authorization": f"Bearer {auth_setup['operator_token']}"},
        )

        assert response.status_code == 403

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_viewer_cannot_delete_archive(
        self, async_client: AsyncClient, auth_setup, archive_factory, printer_factory, db_session
    ):
        """Viewer with no delete permissions cannot delete any archive."""
        printer = await printer_factory()
        archive = await archive_factory(printer.id, print_name="Any Archive")

        response = await async_client.delete(
            f"/api/v1/archives/{archive.id}",
            headers={"Authorization": f"Bearer {auth_setup['viewer_token']}"},
        )

        assert response.status_code == 403

    # ========================================================================
    # UPDATE permissions
    # ========================================================================

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_admin_can_update_any_archive(
        self, async_client: AsyncClient, auth_setup, archive_factory, printer_factory, db_session
    ):
        """Admin can update any archive."""
        printer = await printer_factory()
        archive = await archive_factory(
            printer.id,
            print_name="Original Name",
            created_by_id=auth_setup["operator_user"]["id"],
        )

        response = await async_client.patch(
            f"/api/v1/archives/{archive.id}",
            headers={"Authorization": f"Bearer {auth_setup['admin_token']}"},
            json={"print_name": "Admin Updated"},
        )

        assert response.status_code == 200
        assert response.json()["print_name"] == "Admin Updated"

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_operator_can_update_own_archive(
        self, async_client: AsyncClient, auth_setup, archive_factory, printer_factory, db_session
    ):
        """Operator can update their own archive."""
        printer = await printer_factory()
        archive = await archive_factory(
            printer.id,
            print_name="Original Name",
            created_by_id=auth_setup["operator_user"]["id"],
        )

        response = await async_client.patch(
            f"/api/v1/archives/{archive.id}",
            headers={"Authorization": f"Bearer {auth_setup['operator_token']}"},
            json={"print_name": "Operator Updated"},
        )

        assert response.status_code == 200
        assert response.json()["print_name"] == "Operator Updated"

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_operator_cannot_update_others_archive(
        self, async_client: AsyncClient, auth_setup, archive_factory, printer_factory, db_session
    ):
        """Operator cannot update another user's archive."""
        printer = await printer_factory()
        archive = await archive_factory(
            printer.id,
            print_name="Other's Archive",
            created_by_id=auth_setup["operator2_user"]["id"],
        )

        response = await async_client.patch(
            f"/api/v1/archives/{archive.id}",
            headers={"Authorization": f"Bearer {auth_setup['operator_token']}"},
            json={"print_name": "Attempted Update"},
        )

        assert response.status_code == 403

    # ========================================================================
    # REPRINT permissions
    # ========================================================================

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_operator_cannot_reprint_others_archive(
        self, async_client: AsyncClient, auth_setup, archive_factory, printer_factory, db_session
    ):
        """Operator cannot reprint another user's archive."""
        printer = await printer_factory()
        archive = await archive_factory(
            printer.id,
            created_by_id=auth_setup["operator2_user"]["id"],
        )

        response = await async_client.post(
            f"/api/v1/archives/{archive.id}/reprint?printer_id={printer.id}",
            headers={"Authorization": f"Bearer {auth_setup['operator_token']}"},
        )

        assert response.status_code == 403


class TestQueueOwnershipPermissions(TestOwnershipPermissionsSetup):
    """Tests for print queue ownership-based permissions."""

    @pytest.fixture
    async def queue_item_factory(self, db_session, printer_factory, archive_factory):
        """Factory to create test queue items."""

        async def _create_item(**kwargs):
            from backend.app.models.print_queue import PrintQueueItem
            from backend.app.models.printer_queue import PrinterQueue

            printer = await printer_factory()
            # Create printer queue (queue_id == printer_id)
            queue = PrinterQueue(printer_id=printer.id)
            db_session.add(queue)
            await db_session.commit()
            await db_session.refresh(queue)

            # Create an archive to link to the queue item
            archive = await archive_factory(printer.id)

            defaults = {
                "queue_id": queue.id,
                "archive_id": archive.id,
                "status": "pending",
                "position": 0,
            }
            defaults.update(kwargs)

            item = PrintQueueItem(**defaults)
            db_session.add(item)
            await db_session.commit()
            await db_session.refresh(item)
            return item

        return _create_item

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_admin_can_delete_any_queue_item(self, async_client: AsyncClient, auth_setup, queue_item_factory):
        """Admin can delete any queue item."""
        item = await queue_item_factory(created_by_id=auth_setup["operator_user"]["id"])

        response = await async_client.delete(
            f"/api/v1/queue/{item.id}",
            headers={"Authorization": f"Bearer {auth_setup['admin_token']}"},
        )

        assert response.status_code == 200

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_operator_can_delete_own_queue_item(self, async_client: AsyncClient, auth_setup, queue_item_factory):
        """Operator can delete their own queue item."""
        item = await queue_item_factory(created_by_id=auth_setup["operator_user"]["id"])

        response = await async_client.delete(
            f"/api/v1/queue/{item.id}",
            headers={"Authorization": f"Bearer {auth_setup['operator_token']}"},
        )

        assert response.status_code == 200

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_operator_cannot_delete_others_queue_item(
        self, async_client: AsyncClient, auth_setup, queue_item_factory
    ):
        """Operator cannot delete another user's queue item."""
        item = await queue_item_factory(created_by_id=auth_setup["operator2_user"]["id"])

        response = await async_client.delete(
            f"/api/v1/queue/{item.id}",
            headers={"Authorization": f"Bearer {auth_setup['operator_token']}"},
        )

        assert response.status_code == 403

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_operator_can_update_own_queue_item(self, async_client: AsyncClient, auth_setup, queue_item_factory):
        """Operator can update their own queue item."""
        item = await queue_item_factory(created_by_id=auth_setup["operator_user"]["id"])

        response = await async_client.patch(
            f"/api/v1/queue/{item.id}",
            headers={"Authorization": f"Bearer {auth_setup['operator_token']}"},
            json={"position": 10},
        )

        assert response.status_code == 200

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_operator_cannot_update_others_queue_item(
        self, async_client: AsyncClient, auth_setup, queue_item_factory
    ):
        """Operator cannot update another user's queue item."""
        item = await queue_item_factory(created_by_id=auth_setup["operator2_user"]["id"])

        response = await async_client.patch(
            f"/api/v1/queue/{item.id}",
            headers={"Authorization": f"Bearer {auth_setup['operator_token']}"},
            json={"position": 10},
        )

        assert response.status_code == 403

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_operator_cannot_cancel_others_queue_item(
        self, async_client: AsyncClient, auth_setup, queue_item_factory
    ):
        """Operator cannot cancel another user's queue item."""
        item = await queue_item_factory(created_by_id=auth_setup["operator2_user"]["id"])

        response = await async_client.post(
            f"/api/v1/queue/{item.id}/cancel",
            headers={"Authorization": f"Bearer {auth_setup['operator_token']}"},
        )

        assert response.status_code == 403

    # ------------------------------------------------------------------
    # Start / Stop ownership gates (upstream #1625-followup)
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_operator_cannot_start_others_queue_item(
        self, async_client: AsyncClient, auth_setup, queue_item_factory
    ):
        """_OWN holder cannot start another user's queue item (was an IDOR: /start
        previously required only QUEUE_UPDATE_OWN with no ownership check)."""
        item = await queue_item_factory(created_by_id=auth_setup["operator2_user"]["id"])

        response = await async_client.post(
            f"/api/v1/queue/{item.id}/start",
            headers={"Authorization": f"Bearer {auth_setup['operator_token']}"},
        )

        assert response.status_code == 403

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_operator_can_start_own_queue_item(self, async_client: AsyncClient, auth_setup, queue_item_factory):
        """_OWN holder can start their own queue item."""
        item = await queue_item_factory(created_by_id=auth_setup["operator_user"]["id"], manual_start=True)

        response = await async_client.post(
            f"/api/v1/queue/{item.id}/start",
            headers={"Authorization": f"Bearer {auth_setup['operator_token']}"},
        )

        assert response.status_code == 200

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_operator_can_start_and_claim_ownerless_queue_item(
        self, async_client: AsyncClient, auth_setup, queue_item_factory, db_session
    ):
        """_OWN holder can start a NULL-owner item and is credited as owner
        (VP-import claim flow, #1670)."""
        item = await queue_item_factory(created_by_id=None, manual_start=True)

        response = await async_client.post(
            f"/api/v1/queue/{item.id}/start",
            headers={"Authorization": f"Bearer {auth_setup['operator_token']}"},
        )

        assert response.status_code == 200
        await db_session.refresh(item)
        assert item.created_by_id == auth_setup["operator_user"]["id"]

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_operator_cannot_stop_others_queue_item(
        self, async_client: AsyncClient, auth_setup, queue_item_factory
    ):
        """_OWN holder cannot stop another user's actively-printing item (was
        admin-only: /stop previously required QUEUE_UPDATE_ALL)."""
        item = await queue_item_factory(created_by_id=auth_setup["operator2_user"]["id"], status="printing")

        response = await async_client.post(
            f"/api/v1/queue/{item.id}/stop",
            headers={"Authorization": f"Bearer {auth_setup['operator_token']}"},
        )

        assert response.status_code == 403

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_operator_cannot_stop_ownerless_queue_item(
        self, async_client: AsyncClient, auth_setup, queue_item_factory
    ):
        """Stop is strict — a NULL-owner item requires _ALL (unlike /start it is
        not claimable, stopping is destructive)."""
        item = await queue_item_factory(created_by_id=None, status="printing")

        response = await async_client.post(
            f"/api/v1/queue/{item.id}/stop",
            headers={"Authorization": f"Bearer {auth_setup['operator_token']}"},
        )

        assert response.status_code == 403

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_operator_can_stop_own_queue_item(self, async_client: AsyncClient, auth_setup, queue_item_factory):
        """_OWN holder can stop their own actively-printing item."""
        item = await queue_item_factory(created_by_id=auth_setup["operator_user"]["id"], status="printing")

        response = await async_client.post(
            f"/api/v1/queue/{item.id}/stop",
            headers={"Authorization": f"Bearer {auth_setup['operator_token']}"},
        )

        assert response.status_code == 200

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_bulk_update_skips_non_owned_items(self, async_client: AsyncClient, auth_setup, queue_item_factory):
        """Bulk update only updates items the user owns."""
        # Create items owned by different users
        own_item = await queue_item_factory(
            created_by_id=auth_setup["operator_user"]["id"],
        )
        other_item = await queue_item_factory(
            created_by_id=auth_setup["operator2_user"]["id"],
        )

        response = await async_client.patch(
            "/api/v1/queue/bulk",
            headers={"Authorization": f"Bearer {auth_setup['operator_token']}"},
            json={
                "item_ids": [own_item.id, other_item.id],
                "manual_start": True,
            },
        )

        assert response.status_code == 200
        result = response.json()
        # Should only update the owned item
        assert result["updated_count"] == 1
        assert result["skipped_count"] == 1


class TestLibraryOwnershipPermissions(TestOwnershipPermissionsSetup):
    """Tests for library file ownership-based permissions."""

    @pytest.fixture
    async def library_file_factory(self, db_session):
        """Factory to create test library files."""
        _counter = [0]

        async def _create_file(**kwargs):
            from backend.app.models.library import LibraryFile

            _counter[0] += 1
            defaults = {
                "filename": f"test_{_counter[0]}.3mf",
                "file_path": f"library/test_{_counter[0]}.3mf",
                "file_type": "3mf",
                "file_size": 1024,
            }
            defaults.update(kwargs)

            file = LibraryFile(**defaults)
            db_session.add(file)
            await db_session.commit()
            await db_session.refresh(file)
            return file

        return _create_file

    @pytest.fixture
    async def library_folder_factory(self, db_session):
        """Factory to create test library folders."""
        _counter = [0]

        async def _create_folder(**kwargs):
            from backend.app.models.library import LibraryFolder

            _counter[0] += 1
            defaults = {
                "name": f"TestFolder_{_counter[0]}",
            }
            defaults.update(kwargs)

            folder = LibraryFolder(**defaults)
            db_session.add(folder)
            await db_session.commit()
            await db_session.refresh(folder)
            return folder

        return _create_folder

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_admin_can_delete_any_library_file(self, async_client: AsyncClient, auth_setup, library_file_factory):
        """Admin can delete any library file."""
        file = await library_file_factory(created_by_id=auth_setup["operator_user"]["id"])

        response = await async_client.delete(
            f"/api/v1/library/files/{file.id}",
            headers={"Authorization": f"Bearer {auth_setup['admin_token']}"},
        )

        assert response.status_code == 200

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_operator_can_delete_own_library_file(
        self, async_client: AsyncClient, auth_setup, library_file_factory
    ):
        """Operator can delete their own library file."""
        file = await library_file_factory(created_by_id=auth_setup["operator_user"]["id"])

        response = await async_client.delete(
            f"/api/v1/library/files/{file.id}",
            headers={"Authorization": f"Bearer {auth_setup['operator_token']}"},
        )

        assert response.status_code == 200

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_operator_cannot_delete_others_library_file(
        self, async_client: AsyncClient, auth_setup, library_file_factory
    ):
        """Operator cannot delete another user's library file."""
        file = await library_file_factory(created_by_id=auth_setup["operator2_user"]["id"])

        response = await async_client.delete(
            f"/api/v1/library/files/{file.id}",
            headers={"Authorization": f"Bearer {auth_setup['operator_token']}"},
        )

        assert response.status_code == 403

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_operator_can_update_own_library_file(
        self, async_client: AsyncClient, auth_setup, library_file_factory
    ):
        """Operator can update their own library file."""
        file = await library_file_factory(created_by_id=auth_setup["operator_user"]["id"])

        response = await async_client.put(
            f"/api/v1/library/files/{file.id}",
            headers={"Authorization": f"Bearer {auth_setup['operator_token']}"},
            json={"filename": "renamed.3mf"},
        )

        assert response.status_code == 200

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_operator_cannot_update_others_library_file(
        self, async_client: AsyncClient, auth_setup, library_file_factory
    ):
        """Operator cannot update another user's library file."""
        file = await library_file_factory(created_by_id=auth_setup["operator2_user"]["id"])

        response = await async_client.put(
            f"/api/v1/library/files/{file.id}",
            headers={"Authorization": f"Bearer {auth_setup['operator_token']}"},
            json={"filename": "renamed.3mf"},
        )

        assert response.status_code == 403

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_folders_require_all_permission(self, async_client: AsyncClient, auth_setup, library_folder_factory):
        """Folders require *_all permission (no ownership tracking on folders)."""
        folder = await library_folder_factory(name="TestFolder")

        # Operator cannot delete folder (needs *_all)
        response = await async_client.delete(
            f"/api/v1/library/folders/{folder.id}",
            headers={"Authorization": f"Bearer {auth_setup['operator_token']}"},
        )

        assert response.status_code == 403

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_bulk_delete_skips_non_owned_files(self, async_client: AsyncClient, auth_setup, library_file_factory):
        """Bulk delete only deletes files the user owns."""
        own_file = await library_file_factory(
            filename="own.3mf",
            created_by_id=auth_setup["operator_user"]["id"],
        )
        other_file = await library_file_factory(
            filename="other.3mf",
            created_by_id=auth_setup["operator2_user"]["id"],
        )

        response = await async_client.post(
            "/api/v1/library/bulk-delete",
            headers={"Authorization": f"Bearer {auth_setup['operator_token']}"},
            json={"file_ids": [own_file.id, other_file.id], "folder_ids": []},
        )

        assert response.status_code == 200
        result = response.json()
        # Should only delete the owned file; other_file is skipped (but skipped count not in response)
        assert result["deleted_files"] == 1


class TestAuthDisabledPermissions:
    """Tests that verify all operations are allowed when auth is disabled."""

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_delete_archive_without_auth(
        self, async_client: AsyncClient, archive_factory, printer_factory, db_session
    ):
        """When auth is disabled, anyone can delete archives."""
        printer = await printer_factory()
        archive = await archive_factory(printer.id)

        response = await async_client.delete(f"/api/v1/archives/{archive.id}")

        assert response.status_code == 200

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_update_archive_without_auth(
        self, async_client: AsyncClient, archive_factory, printer_factory, db_session
    ):
        """When auth is disabled, anyone can update archives."""
        printer = await printer_factory()
        archive = await archive_factory(printer.id)

        response = await async_client.patch(
            f"/api/v1/archives/{archive.id}",
            json={"print_name": "Updated Name"},
        )

        assert response.status_code == 200


class TestUserItemsCountAndDeletion(TestOwnershipPermissionsSetup):
    """Tests for user items count endpoint and deletion with items."""

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_get_user_items_count(
        self, async_client: AsyncClient, auth_setup, archive_factory, printer_factory, db_session
    ):
        """Verify items count endpoint returns correct counts."""
        printer = await printer_factory()
        user_id = auth_setup["operator_user"]["id"]

        # Create some items for the operator
        await archive_factory(printer.id, created_by_id=user_id)
        await archive_factory(printer.id, created_by_id=user_id)

        response = await async_client.get(
            f"/api/v1/users/{user_id}/items-count",
            headers={"Authorization": f"Bearer {auth_setup['admin_token']}"},
        )

        assert response.status_code == 200
        counts = response.json()
        assert counts["archives"] >= 2
        assert "queue_items" in counts
        assert "library_files" in counts

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_delete_user_keeps_items(
        self, async_client: AsyncClient, auth_setup, archive_factory, printer_factory, db_session
    ):
        """Verify deleting user without delete_items keeps items (ownerless)."""
        printer = await printer_factory()
        user_id = auth_setup["operator2_user"]["id"]

        # Create archive for operator2
        archive = await archive_factory(printer.id, created_by_id=user_id)
        archive_id = archive.id

        # Delete user without deleting items
        response = await async_client.delete(
            f"/api/v1/users/{user_id}?delete_items=false",
            headers={"Authorization": f"Bearer {auth_setup['admin_token']}"},
        )

        assert response.status_code == 204

        # Verify archive still exists but is now ownerless
        archive_response = await async_client.get(
            f"/api/v1/archives/{archive_id}",
            headers={"Authorization": f"Bearer {auth_setup['admin_token']}"},
        )
        assert archive_response.status_code == 200
        assert archive_response.json()["created_by_id"] is None

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_delete_user_with_items(
        self, async_client: AsyncClient, auth_setup, archive_factory, printer_factory, db_session
    ):
        """Verify deleting user with delete_items=true removes their items."""
        printer = await printer_factory()

        # Create a new user with items
        create_response = await async_client.post(
            "/api/v1/users/",
            headers={"Authorization": f"Bearer {auth_setup['admin_token']}"},
            json={
                "username": "deletewithitems",
                "password": "Password123!",
            },
        )
        user_id = create_response.json()["id"]

        # Create archive for this user
        archive = await archive_factory(printer.id, created_by_id=user_id)
        archive_id = archive.id

        # Delete user WITH deleting items
        response = await async_client.delete(
            f"/api/v1/users/{user_id}?delete_items=true",
            headers={"Authorization": f"Bearer {auth_setup['admin_token']}"},
        )

        assert response.status_code == 204

        # Verify archive was deleted
        archive_response = await async_client.get(
            f"/api/v1/archives/{archive_id}",
            headers={"Authorization": f"Bearer {auth_setup['admin_token']}"},
        )
        assert archive_response.status_code == 404


# Every archive WRITE sub-resource route: (id, http method, path suffix, request kwargs).
# The ownership gate (_ensure_archive_visible) fires immediately after the fetch,
# before any resource-specific logic, so a not-owned / ownerless row 404s regardless
# of whether the timelapse / photo / source / f3d actually exists. Upload routes still
# need a body so FastAPI reaches the handler instead of 422-ing on the missing File(...).
_WRITE_SUBRESOURCE_ROUTES = [
    ("favorite", "post", "/favorite", {}),
    ("timelapse_delete", "delete", "/timelapse", {}),
    ("photo_upload", "post", "/photos", {"files": {"file": ("x.jpg", b"\x89PNG\r\n\x1a\n", "image/jpeg")}}),
    ("photo_delete", "delete", "/photos/nonexistent.jpg", {}),
    ("project_page", "patch", "/project-page", {"json": {"title": "hijacked"}}),
    ("source_upload", "post", "/source", {"files": {"file": ("x.3mf", b"PK\x03\x04", "application/octet-stream")}}),
    ("source_delete", "delete", "/source", {}),
    ("f3d_upload", "post", "/f3d", {"files": {"file": ("x.f3d", b"f3d-bytes", "application/octet-stream")}}),
    ("f3d_delete", "delete", "/f3d", {}),
]


class TestWriteSubResourceIDORClosure(TestOwnershipPermissionsSetup):
    """Regression tests for the archive write SUB-RESOURCE IDOR (upstream security #5).

    The read sub-resource routes were closed under security #2 via
    ``_ensure_archive_visible``, but the *write* sub-resource routes (favorite,
    timelapse, photos, project-page, source, f3d) were left gating on the bare
    ``RequirePermission(ARCHIVES_*_OWN)`` scope and fetched the row by id only —
    never comparing ``created_by_id`` to the caller. An operator holding only
    ``ARCHIVES_*_OWN`` could delete/overwrite files on ANY user's archive, most
    severely rewriting the project-page metadata inside another user's ``.3mf``
    on disk. Each route is now gated by ``require_ownership_permission`` +
    ``_ensure_archive_visible`` → 404 (not 403, to stay non-enumerable and match
    the read side) on a not-owned or ownerless row.
    """

    @pytest.mark.parametrize(
        "name,method,suffix,kwargs",
        _WRITE_SUBRESOURCE_ROUTES,
        ids=[r[0] for r in _WRITE_SUBRESOURCE_ROUTES],
    )
    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_operator_cannot_write_others_archive_subresource(
        self,
        async_client: AsyncClient,
        auth_setup,
        archive_factory,
        printer_factory,
        db_session,
        name,
        method,
        suffix,
        kwargs,
    ):
        """Right credentials, wrong ownership → 404.

        operator1 (ARCHIVES_*_OWN) targeting a route on admin's archive.
        """
        printer = await printer_factory()
        archive = await archive_factory(
            printer.id,
            print_name="Admin's Archive",
            created_by_id=auth_setup["admin_user"]["id"],
        )
        response = await getattr(async_client, method)(
            f"/api/v1/archives/{archive.id}{suffix}",
            headers={"Authorization": f"Bearer {auth_setup['operator_token']}"},
            **kwargs,
        )
        assert response.status_code == 404, f"{name}: expected 404, got {response.status_code}"

    @pytest.mark.parametrize(
        "name,method,suffix,kwargs",
        _WRITE_SUBRESOURCE_ROUTES,
        ids=[r[0] for r in _WRITE_SUBRESOURCE_ROUTES],
    )
    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_operator_cannot_write_ownerless_archive_subresource(
        self,
        async_client: AsyncClient,
        auth_setup,
        archive_factory,
        printer_factory,
        db_session,
        name,
        method,
        suffix,
        kwargs,
    ):
        """Ownerless rows (created_by_id = null, legacy data) require *_ALL — an
        operator with only *_OWN has no 'I own this' claim, so fail closed → 404."""
        printer = await printer_factory()
        archive = await archive_factory(
            printer.id,
            print_name="Ownerless Archive",
            created_by_id=None,
        )
        response = await getattr(async_client, method)(
            f"/api/v1/archives/{archive.id}{suffix}",
            headers={"Authorization": f"Bearer {auth_setup['operator_token']}"},
            **kwargs,
        )
        assert response.status_code == 404, f"{name}: expected 404, got {response.status_code}"

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_operator_can_favorite_own_archive(
        self, async_client: AsyncClient, auth_setup, archive_factory, printer_factory, db_session
    ):
        """Positive control: the owner still gets through the new gate. Favorite
        is the one write sub-resource that needs no pre-existing file, so it
        cleanly proves the *_OWN happy path returns 200 (not a false 404)."""
        printer = await printer_factory()
        archive = await archive_factory(
            printer.id,
            print_name="Operator's Own",
            created_by_id=auth_setup["operator_user"]["id"],
        )
        response = await async_client.post(
            f"/api/v1/archives/{archive.id}/favorite",
            headers={"Authorization": f"Bearer {auth_setup['operator_token']}"},
        )
        assert response.status_code == 200
        assert response.json()["is_favorite"] is True

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_admin_can_favorite_any_archive(
        self, async_client: AsyncClient, auth_setup, archive_factory, printer_factory, db_session
    ):
        """Positive control for the *_ALL path: admin can act on a user's archive."""
        printer = await printer_factory()
        archive = await archive_factory(
            printer.id,
            print_name="Operator's Own",
            created_by_id=auth_setup["operator_user"]["id"],
        )
        response = await async_client.post(
            f"/api/v1/archives/{archive.id}/favorite",
            headers={"Authorization": f"Bearer {auth_setup['admin_token']}"},
        )
        assert response.status_code == 200


class TestSliceOwnershipPermissions(TestOwnershipPermissionsSetup):
    """IDOR regression: slicing and slice-job polling must honour per-row ownership
    (upstream security #6).

    Before the fix, ``POST /library/files/{id}/slice`` and
    ``POST /archives/{id}/slice`` gated only on ``LIBRARY_UPLOAD``, so a READ_OWN
    operator could slice another user's model by raw id even though a direct GET
    on that id returned 404 — the sliced output was then attributed to and
    downloadable by the requester. ``GET /slice-jobs/{id}`` had no owner scoping
    at all. ``POST /slicer-pipelines/{id}/run`` (and check-eligibility) resolved
    the source by raw id with the same gap.

    The slice route enforces the gate before touching the source bytes, so the
    owner/READ_ALL "control" cases reach the later on-disk check (a distinct 404
    detail) rather than a real slice — enough to prove the gate lets them past.
    """

    # Any preset triplet: the ownership 404 fires before preset resolution.
    _SLICE_BODY = {"printer_preset_id": 1, "process_preset_id": 2, "filament_preset_id": 3}

    @pytest.fixture
    async def library_file_factory(self, db_session):
        _counter = [0]

        async def _create_file(**kwargs):
            from backend.app.models.library import LibraryFile

            _counter[0] += 1
            defaults = {
                "filename": f"slice_src_{_counter[0]}.3mf",
                "file_path": f"library/slice_src_{_counter[0]}.3mf",
                "file_type": "3mf",
                "file_size": 1024,
            }
            defaults.update(kwargs)
            row = LibraryFile(**defaults)
            db_session.add(row)
            await db_session.commit()
            await db_session.refresh(row)
            return row

        return _create_file

    @pytest.fixture
    async def readown_slicer(self, async_client, auth_setup):
        """A user restricted to READ_OWN (plus ``library:upload`` to reach the
        slice handler). BamDude's default Operators group carries READ_ALL —
        trusted farm staff who may legitimately read every archive/library row —
        so the realistic IDOR-exposed identity is a custom READ_OWN group, the
        same shape the pipeline-runner test uses. Returns token + user id."""
        admin_headers = {"Authorization": f"Bearer {auth_setup['admin_token']}"}
        group_resp = await async_client.post(
            "/api/v1/groups/",
            headers=admin_headers,
            json={
                "name": "readown_slicers",
                "permissions": ["library:upload", "library:read_own", "archives:read_own"],
            },
        )
        assert group_resp.status_code == 201, group_resp.text
        group_id = group_resp.json()["id"]
        user_resp = await async_client.post(
            "/api/v1/users/",
            headers=admin_headers,
            json={"username": "readown_slicer", "password": "Slicerpass1!", "group_ids": [group_id]},
        )
        assert user_resp.status_code in (200, 201), user_resp.text
        user_id = user_resp.json()["id"]
        login = await async_client.post(
            "/api/v1/auth/login",
            json={"username": "readown_slicer", "password": "Slicerpass1!"},
        )
        return {"token": login.json()["access_token"], "user_id": user_id}

    # --- library file slice ------------------------------------------------

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_readown_cannot_slice_others_library_file(
        self, async_client, auth_setup, library_file_factory, readown_slicer
    ):
        file = await library_file_factory(created_by_id=auth_setup["operator2_user"]["id"])
        resp = await async_client.post(
            f"/api/v1/library/files/{file.id}/slice",
            headers={"Authorization": f"Bearer {readown_slicer['token']}"},
            json=self._SLICE_BODY,
        )
        assert resp.status_code == 404
        # 404 (not 403) so a probing caller can't tell the id exists.
        assert resp.json()["detail"] == "File not found"

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_readown_can_slice_own_library_file(
        self, async_client, auth_setup, library_file_factory, readown_slicer
    ):
        file = await library_file_factory(created_by_id=readown_slicer["user_id"])
        resp = await async_client.post(
            f"/api/v1/library/files/{file.id}/slice",
            headers={"Authorization": f"Bearer {readown_slicer['token']}"},
            json=self._SLICE_BODY,
        )
        # Past the ownership gate — only the on-disk source is missing in tests.
        assert resp.status_code == 404
        assert resp.json()["detail"] == "Source file missing on disk"

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_admin_can_slice_any_library_file(self, async_client, auth_setup, library_file_factory):
        file = await library_file_factory(created_by_id=auth_setup["operator2_user"]["id"])
        resp = await async_client.post(
            f"/api/v1/library/files/{file.id}/slice",
            headers={"Authorization": f"Bearer {auth_setup['admin_token']}"},
            json=self._SLICE_BODY,
        )
        # READ_ALL passes the gate even on another user's file.
        assert resp.status_code == 404
        assert resp.json()["detail"] == "Source file missing on disk"

    # --- archive slice -----------------------------------------------------

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_admin_can_slice_any_archive(self, async_client, auth_setup, archive_factory, printer_factory):
        printer = await printer_factory()
        archive = await archive_factory(printer.id, created_by_id=auth_setup["operator2_user"]["id"])
        resp = await async_client.post(
            f"/api/v1/archives/{archive.id}/slice",
            headers={"Authorization": f"Bearer {auth_setup['admin_token']}"},
            json=self._SLICE_BODY,
        )
        # READ_ALL passes the gate even on another user's archive.
        assert resp.status_code == 404
        assert resp.json()["detail"] == "Archive source file missing on disk"

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_readown_cannot_slice_others_archive(
        self, async_client, auth_setup, archive_factory, printer_factory, readown_slicer
    ):
        printer = await printer_factory()
        archive = await archive_factory(printer.id, created_by_id=auth_setup["operator2_user"]["id"])
        resp = await async_client.post(
            f"/api/v1/archives/{archive.id}/slice",
            headers={"Authorization": f"Bearer {readown_slicer['token']}"},
            json=self._SLICE_BODY,
        )
        assert resp.status_code == 404
        assert resp.json()["detail"] == "Archive not found"

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_readown_can_slice_own_archive(
        self, async_client, auth_setup, archive_factory, printer_factory, readown_slicer
    ):
        printer = await printer_factory()
        archive = await archive_factory(printer.id, created_by_id=readown_slicer["user_id"])
        resp = await async_client.post(
            f"/api/v1/archives/{archive.id}/slice",
            headers={"Authorization": f"Bearer {readown_slicer['token']}"},
            json=self._SLICE_BODY,
        )
        # Past the gate — the archive's source file isn't on disk in tests.
        assert resp.status_code == 404
        assert resp.json()["detail"] == "Archive source file missing on disk"

    # --- slice-job polling -------------------------------------------------

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_slice_job_polling_is_owner_scoped(self, async_client, auth_setup, readown_slicer):
        from backend.app.services.slice_dispatch import slice_dispatch

        async def _noop(_job_id):
            return {}

        # A job owned by someone else. The READ_OWN caller can't see it (404,
        # not 403); a READ_ALL admin can.
        others_job = await slice_dispatch.enqueue(
            kind="library_file",
            source_id=1,
            source_name="secret_model.3mf",
            owner_id=auth_setup["operator2_user"]["id"],
            run=_noop,
        )
        other = await async_client.get(
            f"/api/v1/slice-jobs/{others_job.id}",
            headers={"Authorization": f"Bearer {readown_slicer['token']}"},
        )
        assert other.status_code == 404
        admin = await async_client.get(
            f"/api/v1/slice-jobs/{others_job.id}",
            headers={"Authorization": f"Bearer {auth_setup['admin_token']}"},
        )
        assert admin.status_code == 200

        # A job owned by the READ_OWN caller — they see it via the ownership
        # branch (they have no READ_ALL, so this proves per-row matching, not a
        # blanket bypass).
        own_job = await slice_dispatch.enqueue(
            kind="library_file",
            source_id=2,
            source_name="my_model.3mf",
            owner_id=readown_slicer["user_id"],
            run=_noop,
        )
        mine = await async_client.get(
            f"/api/v1/slice-jobs/{own_job.id}",
            headers={"Authorization": f"Bearer {readown_slicer['token']}"},
        )
        assert mine.status_code == 200

    # --- pipeline source resolution ----------------------------------------

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_pipeline_run_cannot_reference_others_library_file(
        self, async_client, auth_setup, library_file_factory, db_session
    ):
        """A pipeline runner with READ_OWN cannot resolve another user's source.

        The built-in Operators group has no pipeline permissions, so this uses a
        custom group carrying PIPELINES_RUN + READ_OWN — the realistic shape of
        the exposure. check-eligibility resolves the source before any
        eligibility work, so the ownership gate is what returns 404.
        """
        from backend.app.models.slicer_pipeline import SlicerPipeline

        admin_headers = {"Authorization": f"Bearer {auth_setup['admin_token']}"}
        group_resp = await async_client.post(
            "/api/v1/groups/",
            headers=admin_headers,
            json={
                "name": "pipeline_runners",
                "permissions": [
                    "pipelines:read",
                    "pipelines:run",
                    "library:read_own",
                    "archives:read_own",
                ],
            },
        )
        assert group_resp.status_code == 201, group_resp.text
        group_id = group_resp.json()["id"]

        await async_client.post(
            "/api/v1/users/",
            headers=admin_headers,
            json={"username": "runner1", "password": "Runnerpass1!", "group_ids": [group_id]},
        )
        runner_login = await async_client.post(
            "/api/v1/auth/login",
            json={"username": "runner1", "password": "Runnerpass1!"},
        )
        runner_token = runner_login.json()["access_token"]

        pipeline = SlicerPipeline(
            name="Cross-user pipeline",
            printer_preset_source="local",
            printer_preset_id="1",
            process_preset_source="local",
            process_preset_id="2",
            filament_presets_json="[]",
            target_kind="printer_class",
            target_model_class="Bambu Lab X1 Carbon",
        )
        db_session.add(pipeline)
        await db_session.commit()
        await db_session.refresh(pipeline)

        # Source owned by operator2, not the runner.
        file = await library_file_factory(created_by_id=auth_setup["operator2_user"]["id"])
        resp = await async_client.post(
            f"/api/v1/slicer-pipelines/{pipeline.id}/check-eligibility",
            headers={"Authorization": f"Bearer {runner_token}"},
            json={"source_library_file_id": file.id},
        )
        assert resp.status_code == 404
        assert resp.json()["detail"] == "File not found"
