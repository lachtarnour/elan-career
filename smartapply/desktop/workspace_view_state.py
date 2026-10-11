"""View revisions and coalesced navigation shared by both journal controllers."""


class WorkspaceViewState:
    def _init_view(self, kind):
        self._view_kind = kind
        self._view_revision = 0
        self._candidate_revision = 0
        self._candidates_dirty = True
        self.candidates = []
        self._candidate_notify = False
        self._startup_sync_pending = False
        self._view = {
            "history_page": 1, "selected_run_id": "", "candidate_page": 1,
            "candidate_query": "", "candidate_sort": "published_date", "candidate_ascending": False,
        }

    def set_view(self, **changes):
        if all(self._view.get(key) == value for key, value in changes.items()):
            return
        self._view.update(changes)
        self._view_revision += 1
        if any(key.startswith("candidate_") for key in changes):
            self.invalidate_candidates()
        self.refresh(sync=False)

    def invalidate_candidates(self):
        self._candidate_revision += 1
        self._candidates_dirty = True

    def _view_options(self):
        return {
            **self._view, "view_revision": self._view_revision,
            "candidate_revision": self._candidate_revision,
            "include_candidates": self._candidates_dirty,
            "monitor_run_ids": [run["id"] for run in self._observed_runs(self.workspace) if run.get("is_active")],
        }

    @staticmethod
    def _observed_runs(workspace):
        if not workspace.get("desktop_view"):
            return workspace.get("runs", [])
        runs = {str(run["id"]): run for run in workspace.get("runs", [])}
        runs.update({str(run["id"]): run for run in workspace.get("monitor_runs", [])})
        selected = workspace.get("selected_run") or {}
        if selected.get("id"):
            runs[str(selected["id"])] = selected
        return list(runs.values())

    def _accept_view(self, workspace):
        if not workspace.get("desktop_view"):
            return workspace
        if workspace.get("view_revision") != self._view_revision:
            self._pending_read = True
            return None
        workspace = dict(workspace)
        candidate_keys = (
            "candidate_ids", "matching_candidate_ids", "candidates_count", "pending_count", "candidate_count",
            "candidate_page", "candidate_page_size", "candidate_total_pages", "candidate_revision",
        )
        if "candidates" in workspace and workspace.get("candidate_revision") != self._candidate_revision:
            workspace.pop("candidates")
            for key in candidate_keys:
                workspace.pop(key, None)
            self._pending_read = True
        if "candidates" in workspace:
            candidates = workspace.pop("candidates")
            self._candidates_dirty = False
            if candidates != self.candidates:
                self.candidates = candidates
                self._candidate_notify = True
        for key in candidate_keys:
            if key not in workspace and key in self.workspace:
                workspace[key] = self.workspace[key]
        self._view["selected_run_id"] = str(workspace.get("selected_run", {}).get("id") or "")
        # Reflect clamped pages after deletion, without another navigation read.
        for key in ("history_page", "candidate_page"):
            if key in workspace:
                self._view[key] = workspace[key]
        return workspace
