"""Combine live episode evidence for an explicitly matched Douban work."""
from __future__ import annotations

import copy
import re
import unicodedata


def subject_records(plugin, subject_id, season):
    """Only enabled, unambiguous matches participate; never join by show name."""
    if not subject_id:
        return []
    with plugin._lock:
        stores = {bucket: copy.deepcopy(plugin.get_data(bucket) or {}) for bucket in ("archive", "pending")}
    selected = plugin._selected_servers()
    direct = {row.get("name") for row in (plugin.get_data("direct_servers_v2") or {}).values()}
    result = []
    for bucket, records in stores.items():
        for key, record in records.items():
            if (not str(record.get("type") or "").startswith(("TV", "电视剧"))
                    or str(record.get("subject_id") or "") != subject_id
                    or int(record.get("season") or 0) != season
                    or plugin._manual_guard(key) or not plugin._direct_record_allowed(record)):
                continue
            server = str(record.get("server_name") or "")
            if not server or (selected and server not in direct and server not in selected):
                continue
            result.append((key, record))
    return result


def _episode_title(item):
    name = unicodedata.normalize("NFKC", str(item.get("Name") or "")).strip().casefold()
    series = unicodedata.normalize("NFKC", str(item.get("SeriesName") or "")).strip().casefold()
    if series and name.startswith(series + " "):
        name = name[len(series):].strip()
    name = re.sub(r"^(?:oad|ova|sp)\s*#?\s*\d+(?:[-－]\d+)?\s+", "", name)
    # Generic names are not enough to align differently numbered specials.
    if re.fullmatch(r"(?:第?\s*\d+\s*[集话話]?|episode\s*\d+|special\s*\d+)", name):
        return ""
    return re.sub(r"[\s:：]", "", name)


def _same_episode(left, right):
    a, b = left.get("ProviderIds") or {}, right.get("ProviderIds") or {}
    a = {str(k).casefold(): str(v).strip().casefold() for k, v in a.items() if v}
    b = {str(k).casefold(): str(v).strip().casefold() for k, v in b.items() if v}
    common = set(a) & set(b) & {"tvdb", "imdb", "tmdb"}
    if common:
        return all(a[key] == b[key] for key in common)
    title = _episode_title(left)
    return bool(title and title == _episode_title(right))


def _special_mapping(reference, items):
    """A one-to-one identity match is required; raw S00 numbers are server-local."""
    mapping = {}
    for item in items:
        matches = [row["IndexNumber"] for row in reference if _same_episode(row, item)]
        if len(matches) == 1:
            mapping[item["IndexNumber"]] = matches[0]
    # Ambiguous duplicate metadata must not make two different episodes count as one.
    duplicates = {index for index in mapping.values() if list(mapping.values()).count(index) > 1}
    return {source: target for source, target in mapping.items() if target not in duplicates}


def tv_completion(plugin, payload, reader, subject_id=""):
    season = plugin._to_int(payload.get("season"), 0, 0)
    episode = plugin._to_int(payload.get("episode"), 0, 0)
    server = str(payload.get("server_name") or "")
    expected = plugin._subject_episode_count(subject_id) if subject_id else 0
    sources = []
    unavailable = False

    def read(record, source_reader):
        series = source_reader.get_series_id(str(record.get("item_id") or ""))
        def work_scope(series_id):
            if not series_id:
                return []
            scope = source_reader.get_work_episodes(series_id, season,
                        str(record.get("source_title") or record.get("title") or ""), int(record.get("episode") or 0))
            return sorted({index for index in scope.get("episodes") or [] if isinstance(index, int) and index > 0})
        episodes = work_scope(series)
        if not episodes and season > 0 and subject_id:
            # A library rebuild can replace an old item ID. Re-locate only if
            # the new series explicitly has the same scraped Douban identity.
            candidate = source_reader.search_series(
                str(record.get("subject_name") or record.get("title") or ""), season)
            if candidate and source_reader.get_douban_id(candidate) == subject_id:
                series = candidate
                episodes = work_scope(series)
        if not episodes:
            return None
        state = plugin._play_state_of_any_user(series, season, str(record.get("server_name") or ""))
        state = {index: state[index] for index in episodes if isinstance(state.get(index), bool)}
        if not episodes or not state:
            return None
        return {"server": str(record.get("server_name") or ""), "reader": source_reader,
                "series": series, "episodes": episodes, "state": state}

    try:
        primary = read({**payload, "episode": episode}, reader)
    except Exception:
        primary = None
    if primary:
        sources.append(primary)
    else:
        unavailable = True
    candidates = subject_records(plugin, subject_id, season)
    seen = {server}
    for _, record in sorted(candidates, key=lambda pair: str(pair[1].get("timestamp") or ""), reverse=True):
        name = record["server_name"]
        if name in seen:
            continue
        seen.add(name)
        try:
            peer = read(record, plugin._reader(name, username=record.get("user_name")))
            if peer:
                sources.append(peer)
            else:
                unavailable = True
        except Exception:
            unavailable = True

    unavailable = unavailable or any(len(source["state"]) < len(source["episodes"]) for source in sources)
    if not sources:
        return {"complete": False, "known": False, "episodes": [], "played": [], "expected": expected, "servers": []}
    merged = {}
    if season > 0 or len(sources) == 1:
        for source in sources:
            for index in source["episodes"]:
                if index in source["state"]:
                    merged[index] = merged.get(index, False) or source["state"][index]
        episodes = sorted({index for source in sources for index in source["episodes"]})
    else:
        # Use the most complete catalogue as the numbering reference. Do not
        # invent identities for disjoint or ambiguously named special episodes.
        for source in sources:
            try:
                source["items"] = [row for row in source["reader"].get_season_episode_items(source["series"], season)
                                   if row.get("IndexNumber") in source["episodes"]]
            except Exception:
                source["items"] = []
                unavailable = True
        reference = max(sources, key=lambda source: (len(source["episodes"]), source["server"]))
        episodes = reference["episodes"]
        for source in sources:
            mapping = ({index: index for index in episodes} if source is reference
                       else _special_mapping(reference["items"], source["items"]))
            if len(mapping) < len(source["episodes"]):
                unavailable = True
            for index, target in mapping.items():
                if index in source["state"]:
                    merged[target] = merged.get(target, False) or source["state"][index]
    played = [index for index in episodes if merged.get(index) is True]
    enough = bool(expected and len(episodes) >= expected)
    if season > 0 and expected:
        enough = enough and set(range(1, expected + 1)).issubset(episodes)
    coverage = bool(episodes and enough and all(index in merged for index in episodes))
    complete = bool(coverage and len(played) == len(episodes))
    incomplete = bool(episodes and (any(merged.get(index) is False for index in episodes) or (expected and not enough)))
    # An unavailable peer can hold the missing watched episodes. Do not use a
    # partial read to downgrade a previously completed work.
    known = complete or bool(not unavailable and (coverage or incomplete))
    return {"complete": complete, "known": known, "episodes": episodes, "played": played,
            "expected": expected, "servers": sorted({source["server"] for source in sources if source["server"]})}


def progress_fields(completion):
    return {"work_episodes": completion["episodes"], "watched_episodes": completion["played"],
            "expected_episodes": completion.get("expected", 0), "progress_servers": completion.get("servers", [])}
