"""Keep Douban subject classification separate from the media server layout."""
from __future__ import annotations

import copy
import time

from .episodes import work_title
from .matching import IdentifierMatching


class SubjectRouting(IdentifierMatching):
    @staticmethod
    def _source_kind(record):
        value = str(record.get("source_media_type") or record.get("media_type") or record.get("type") or "")
        return "TV" if value.startswith(("TV", "电视剧")) else "MOV"

    def _movie_episode_key(self, record):
        title = work_title(record.get("source_title") or record.get("title") or "")
        base = f"{title}_S{int(record.get('season') or 0)}E{int(record.get('episode') or 0)}"
        server = str(record.get("server_name") or "")
        return base + ("@" + server if server else "")

    def _routing_key(self, record):
        if self._source_kind(record) == "TV" and int(record.get("episode") or 0) > 0:
            exact = self._movie_episode_key(record)
            if any(exact in (self.get_data(bucket) or {}) for bucket in
                   ("archive", "pending", "manual_matches", "unmatched", "corrections")):
                return exact
        return self._archive_key(record.get("source_title") or record.get("title") or "",
                                 self._source_kind(record), int(record.get("season") or 0), record.get("server_name"))

    def _subject_detail(self, client, subject_id, hint):
        cache = self.get_data("subject_details") or {}
        saved = cache.get(subject_id) or {}
        if saved.get("media_type") in {"MOV", "TV"} and saved.get("expires", 0) > time.time():
            return dict(saved)
        detail = client.get_subject_info(subject_id, hint)
        if (not isinstance(detail, dict) or detail.get("media_type") not in {"MOV", "TV"}) and not saved.get("media_type"):
            if self._prepare_login(client):
                detail = client.get_subject_info(subject_id, hint)
        if isinstance(detail, dict) and detail.get("media_type") in {"MOV", "TV"}:
            with self._lock:
                cache = self.get_data("subject_details") or {}
                cache[subject_id] = {key: detail.get(key) for key in ("title", "image", "media_type", "episodes_count")}
                cache[subject_id].update(expires=time.time() + 21600, source="douban")
                self.save_data("subject_details", cache)
            self._remember_subject_info(subject_id, detail)
            return detail
        # A failed fetch does not turn the media server's type into a Douban type.
        return dict(saved) if saved.get("media_type") in {"MOV", "TV"} else None

    def _bind_subject_kind(self, key, record, kind):
        source = self._source_kind(record)
        result = {**record, "source_media_type": source, "douban_type": kind}
        if kind == "MOV":
            result.update(work_episodes=[], watched_episodes=[], expected_episodes=0, progress_servers=[])
        if source != "TV" or kind != "MOV" or int(record.get("episode") or 0) <= 0:
            return key, result
        new = self._movie_episode_key(record)
        if new == key:
            return key, result
        with self._lock:
            stores = {bucket: copy.deepcopy(self.get_data(bucket) or {}) for bucket in
                      ("archive", "pending", "unmatched", "manual_matches", "corrections")}
            if any(new in rows for rows in stores.values()):
                raise ValueError("电影分集匹配已有记录，请刷新后单独修正")
            if not self.get_data("subject_type_backup_2.3.9"):
                self.save_data("subject_type_backup_2.3.9", stores)
            for bucket, rows in stores.items():
                if key not in rows:
                    continue
                value = rows.pop(key)
                if bucket == "corrections":
                    value["record"] = {**value["record"], **result}
                elif bucket == "manual_matches":
                    value.update(media_type=kind, season=int(record.get("season") or 0), episode=int(record.get("episode") or 0))
                else:
                    value.update(source_media_type=source, douban_type=kind,
                                 type="电影", work_episodes=[], watched_episodes=[], expected_episodes=0, progress_servers=[])
                rows[new] = value
                self.save_data(bucket, rows)
        return new, result

    def _movie_finished(self, record, reader):
        if record.get("event"):
            return self._is_finished_event(record)
        if self._source_kind(record) == "TV" and int(record.get("episode") or 0) > 0:
            series = reader.get_series_id(str(record.get("item_id") or ""))
            return bool(series and self._is_played_by_any_user(
                series, int(record.get("season") or 0), int(record["episode"]),
                record.get("server_name"), record.get("user_name")) is True)
        return record.get("status") == "collect"

    def _defer_old_type_failures(self):
        """Do not turn a code upgrade into an unsolicited retry of failed marks."""
        if self.get_data("subject_type_failures_v1"):
            return
        operations = self.get_data("corrections") or {}
        for operation in operations.values():
            if operation.get("state") == "failed" and not (operation.get("record") or {}).get("douban_type"):
                operation["manual_retry_only"] = True
        self.save_data("corrections", operations)
        self.save_data("subject_type_failures_v1", True)
