"""先用媒体服务器的外部标识确认作品，无结果时严格核对名称、年份、类型及季号。"""
from __future__ import annotations

import re
import time

from .doubanclient import DoubanClient


class IdentifierMatching:
    """
    统一首次同步和失败重试的编号匹配规则。
    """

    def _match_subject(self, payload: dict, reader) -> tuple:
        """
        优先按编号查找豆瓣，无结果时严格核对名称、年份、类型和季号。

        :param payload: 当前观看条目，失败原因及匹配证据写回此字典。
        :param reader: 仅用于读取当前媒体服务器元数据的读取器。
        :return: 已确认的豆瓣标题、编号；未确认时返回空值。
        """
        kind = self._source_kind(payload)
        season = int(payload.get("season") or 0)
        metadata = reader.get_match_metadata(str(payload.get("item_id") or ""), kind,
                                             season, int(payload.get("episode") or 0))
        if not isinstance(metadata, dict) or not metadata:
            metadata = payload.get("match_metadata") or {}
        if not isinstance(metadata, dict):
            metadata = {}
        payload["match_metadata"] = metadata
        imdb = str(metadata.get("imdb_id") or "").lower()
        tmdb = str(metadata.get("tmdb_id") or "")
        if not re.fullmatch(r"tt\d+", imdb):
            imdb = ""
        if not tmdb.isdigit():
            tmdb = ""
        attempts = []
        for index in range(2):
            if index == 0:
                value, method = imdb, "imdb"
            else:
                value, method = self._tmdb_imdb(metadata) if tmdb else "", "tmdb_imdb"
            if not value or value in attempts:
                continue
            attempts.append(value)
            cache_key = f"{value}|{kind}|{season}|{metadata.get('scope', '')}"
            saved = (self.get_data("identifier_matches") or {}).get(cache_key) or {}
            if saved.get("expires", 0) > time.time():
                title, subject = saved.get("title"), saved.get("subject_id")
            else:
                title, subject = DoubanClient().find_by_imdb(value)
                # IMDb 可能指向整部剧或第一季，不能把它直接套到其他季。
                if (subject and kind == "TV" and season > 0
                        and self._matched_season(title) not in ({0, 1} if season == 1 else {season})):
                    continue
                if subject:
                    with self._lock:
                        cache = self.get_data("identifier_matches") or {}
                        cache = {key: item for key, item in cache.items()
                                 if item.get("expires", 0) > time.time()}
                        cache[cache_key] = {"title": title, "subject_id": subject,
                                            "expires": time.time() + 21600}
                        self.save_data("identifier_matches", cache)
            if subject:
                payload.update(match_method=method, matched_imdb=value, subject_error="")
                return title, subject
        if not imdb and not tmdb:
            # 已刮削的豆瓣编号也是显式标识；历史自动搜索结果不属于此来源。
            subject = metadata.get("douban_id")
            if not subject and metadata.get("scope") != "episode" and not (kind == "TV" and season == 0):
                subject = reader.get_douban_id(str(payload.get("item_id") or ""))
            if isinstance(subject, str) and subject.isdigit():
                payload.update(match_method="emby_douban", subject_error="")
                return payload.get("source_title") or payload.get("title"), subject
        payload["match_imdb_candidates"] = attempts
        return self._match_by_title_year(payload, metadata)

    def _match_by_title_year(self, payload: dict, metadata: dict) -> tuple:
        """
        编号无结果时核对名称、年份、类型和季号；候选必须唯一。

        :param payload: 观看记录及失败原因。
        :param metadata: 当前作品的 Emby 元数据。
        :return: 唯一确认的豆瓣标题、编号，或空值。
        """
        from .episodes import work_title
        kind = self._source_kind(payload)
        season = int(payload.get("season") or 0)
        year = self._metadata_year(metadata.get("year"))
        if kind == "TV" and season > 1:
            year = self._season_year(metadata, season)
            if not year:
                payload["subject_error"] = "当前季首播年份未知，不能使用第一季年份匹配，请手动填写豆瓣编号"
                return None, None
        names = [work_title(str(value)) for value in
                 (metadata.get("title"), metadata.get("original_title"), payload.get("source_title") or payload.get("title"))
                 if value and isinstance(value, str)]
        if kind == "TV" and season == 0:
            names = [work_title(str(value)) for value in (metadata.get("title"), metadata.get("original_title"))
                     if value and isinstance(value, str)]
            parent = self._name_key(metadata.get("series_title"), 0)
            names = [name for name in names if self._name_key(name, 0) != parent
                     and not re.fullmatch(r"(?:第?\s*\d+\s*集|episode\s*\d+|special\s*\d+)", name, re.I)]
        names = list(dict.fromkeys(names))[:3]
        if not names or not year:
            payload["subject_error"] = "缺少可靠的作品名称或年份，请手动填写豆瓣编号"
            return None, None
        cache_key = f"{kind}|{season}|{year}|{metadata.get('imdb_id', '')}|{metadata.get('tmdb_id', '')}|" + "|".join(sorted(names))
        saved = (self.get_data("title_year_matches") or {}).get(cache_key) or {}
        if saved.get("expires", 0) > time.time():
            payload.update(match_method="title_year", match_year=year, subject_error="")
            return saved.get("title"), saved.get("subject_id")
        client = DoubanClient()
        expected = {self._name_key(name, season) for name in names}
        expected.update(self._name_key(a + b, season) for a in names for b in names if a != b)
        found, examined = {}, set()
        types = ["TV", "MOV"] if kind == "TV" and season == 0 else [kind]
        for name in names:
            for search_type in types:
                for candidate in client.search_candidates(name, year, search_type, season):
                    subject = str(candidate.get("subject_id") or "")
                    if not subject.isdigit() or subject in examined:
                        continue
                    # 先核对名称，避免为大量不相关的候选请求详情。
                    aliases = [candidate.get("title"), candidate.get("original_title"), *(candidate.get("aliases") or [])]
                    keys = {self._name_key(value, season) for value in aliases if isinstance(value, str) and value}
                    if not expected.intersection(keys):
                        continue
                    if len(examined) >= 12:
                        payload["subject_error"] = "同名候选过多，无法唯一确认，请手动填写豆瓣编号"
                        return None, None
                    examined.add(subject)
                    detail = client.get_subject_info(subject, search_type)
                    if not isinstance(detail, dict):
                        continue
                    candidate = {**candidate, **{key: value for key, value in detail.items() if value not in (None, "", 0, [])}}
                    aliases = [candidate.get("title"), candidate.get("original_title"), *(candidate.get("aliases") or [])]
                    keys = {self._name_key(value, season) for value in aliases if isinstance(value, str) and value}
                    if not expected.intersection(keys) or self._metadata_year(candidate.get("year")) != year:
                        continue
                    if candidate.get("media_type") not in types:
                        continue
                    number = str(candidate.get("season_number") or "")
                    actual_season = (int(number) if number.isdigit() else 0) or self._matched_season(candidate.get("title"))
                    title_season = self._matched_season(candidate.get("title"))
                    if title_season and number.isdigit() and int(number) and title_season != int(number):
                        continue
                    if kind == "TV" and season > 0 and actual_season not in ({0, 1} if season == 1 else {season}):
                        continue
                    # 整部剧与单集 IMDb 口径不同；电影及第一季的明确冲突必须拒绝。
                    known = str(candidate.get("imdb_id") or "").lower()
                    sources = {str(value).lower() for value in (payload.get("match_imdb_candidates") or [])}
                    sources.update(str(metadata.get(key) or "").lower() for key in ["imdb_id"])
                    sources.discard("")
                    if (known and sources and known not in sources
                            and (kind == "MOV" or (kind == "TV" and season == 1))):
                        continue
                    found[subject] = candidate.get("title") or name
        if len(found) != 1:
            payload["subject_error"] = "名称、年份、类型和季号未找到唯一豆瓣条目，请手动匹配或选择无条目"
            return None, None
        subject, title = next(iter(found.items()))
        with self._lock:
            cache = self.get_data("title_year_matches") or {}
            cache = {key: value for key, value in cache.items() if value.get("expires", 0) > time.time()}
            cache[cache_key] = {"title": title, "subject_id": subject, "expires": time.time() + 21600}
            self.save_data("title_year_matches", cache)
        payload.update(match_method="title_year", match_year=year, subject_error="")
        return title, subject

    def _season_year(self, metadata: dict, season: int) -> int:
        """
        优先查询 TMDB 当前季首播年；Emby 与首季相同的年份不作后续季证据。

        :param metadata: Series 编号和 Emby 当前季元数据。
        :param season: 正片当前季号。
        :return: 可确认的当前季年份，未知为 0。
        """
        tmdb = str(metadata.get("tmdb_id") or "")
        if tmdb.isdigit() and metadata.get("scope") == "series":
            key = f"{tmdb}|{season}"
            cached = (self.get_data("season_years") or {}).get(key) or {}
            if cached.get("expires", 0) > time.time():
                return int(cached["year"])
            detail = self._tmdb_detail(metadata, season)
            if isinstance(detail, dict) and detail.get("season_number") == season:
                year = self._metadata_year(detail.get("air_date"))
                if year:
                    with self._lock:
                        cache = self.get_data("season_years") or {}
                        cache[key] = {"year": year, "expires": time.time() + 21600}
                        self.save_data("season_years", cache)
                    return year
        year = self._metadata_year(metadata.get("season_year"))
        return year if year and year != self._metadata_year(metadata.get("year")) else 0

    @staticmethod
    def _metadata_year(value) -> int:
        """
        从年份或首播日期提取合理的公历年份，未知时返回 0。

        :param value: 年份或日期文本。
        :return: 有效年份。
        """
        found = re.match(r"^(\d{4})(?:\D|$)", str(value or ""))
        return int(found[1]) if found and 1800 <= int(found[1]) <= 2200 else 0

    @staticmethod
    def _name_key(value, season: int) -> str:
        """
        规范化完整名称，只移除明确的季号与排版字符，不做模糊包含匹配。

        :param value: 正式名称或别名。
        :param season: 当前季号，普通季允许去掉名称中的季号后比较主标题。
        :return: 完整名称的比较键。
        """
        import unicodedata
        text = unicodedata.normalize("NFKC", str(value or "")).casefold()
        if season > 0:
            text = re.sub(r"第\s*[\d一二三四五六七八九十]+\s*季|season\s*\d+", "", text, flags=re.I)
        return "".join(char for char in text if char.isalnum())

    @staticmethod
    def _tmdb_detail(metadata: dict, season=None):
        """
        通过公共链接口读取作品或指定季的 TMDB 原始详情。

        :param metadata: 已确认类型的 TMDB 编号。
        :param season: 明确查询的季号，None 查询作品。
        :return: 宿主返回的详情字典，失败为空。
        """
        try:
            from app.sdk.chain import ChainBase
            from app.schemas.types import MediaType
            kind = MediaType.TV if metadata.get("media_type") == "TV" else MediaType.MOVIE
            params = {"tmdbid": int(metadata["tmdb_id"]), "mtype": kind}
            if season is not None:
                params["season"] = season
            result = ChainBase().tmdb_info(**params)
            return result if isinstance(result, dict) else None
        except Exception:
            return None

    @staticmethod
    def _tmdb_imdb(metadata: dict) -> str:
        """
        通过宿主 TMDB 查询取得同一作品的 IMDb，保留电影和剧集编号命名空间。

        :param metadata: 来自 Emby 的作品类型、TMDB 编号及标识作用范围。
        :return: 合法的 IMDb 编号；查询不可用或只有单集编号时返回空字符串。
        """
        if metadata.get("scope") == "episode":
            return ""
        detail = IdentifierMatching._tmdb_detail(metadata)
        if not isinstance(detail, dict) or str(detail.get("id") or "") != str(metadata["tmdb_id"]):
            return ""
        external = detail.get("external_ids") or {}
        imdb = str(detail.get("imdb_id") or (external.get("imdb_id") if isinstance(external, dict) else "") or "").lower()
        return imdb if re.fullmatch(r"tt\d+", imdb) else ""

    @staticmethod
    def _matched_season(title: str) -> int:
        """
        提取豆瓣条目明确声明的季号，不把没有季号的主标题当成当前季。

        :param title: IMDb 映射接口返回的豆瓣标题。
        :return: 明确的季号；未知时返回 0。
        """
        text = str(title or "")
        match = re.search(r"第\s*([\d一二三四五六七八九十]+)\s*季|season\s*(\d+)", text, re.I)
        if not match:
            return 0
        value = match.group(1) or match.group(2)
        if value.isdigit():
            return int(value)
        digits = {word: number for number, word in enumerate("零一二三四五六七八九")}
        if "十" in value:
            left, right = value.split("十", 1)
            return (digits.get(left, 1) if left else 1) * 10 + digits.get(right, 0)
        return digits.get(value, 0)
