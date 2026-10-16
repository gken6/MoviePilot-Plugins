"""作品级剧集归组；只合并明确的上下集，不把不同 OAD 混在一起。"""
import re
import unicodedata

_PART = re.compile(r"[（(]\s*([上中下])(?:集|篇|部)?\s*[）)]\s*$")


def work_title(title):
    text = unicodedata.normalize("NFKC", str(title or "")).strip()
    if not _PART.search(text):
        return str(title or "").strip()
    text = _PART.sub("", text).strip()
    # OAD #4-1 / #4-2 是同一作品的分段，#3 与 #4 始终隔离。
    text = re.sub(r"((?:OAD|OVA|SP)\s*#?\s*\d+)\s*[-－]\s*\d+", r"\1", text, flags=re.I)
    return re.sub(r"\s+", " ", text).strip()


def multipart(title):
    return bool(_PART.search(unicodedata.normalize("NFKC", str(title or ""))))


def same_work(left, right):
    left, right = work_title(left), work_title(right)
    # Webhook 会在单集名前加剧名，而 Emby Episodes 返回的 Name 不含剧名。
    return left == right or left.endswith(" " + right) or right.endswith(" " + left)


def work_episodes(items, season, title, episode):
    selected = [item for item in items if isinstance(item, dict)
                and isinstance(item.get("IndexNumber"), int) and item["IndexNumber"] > 0
                and item.get("ParentIndexNumber") == season]
    if season != 0:
        return {"episodes": sorted({item["IndexNumber"] for item in selected}), "minimum": 0}
    current = next((item for item in selected if item["IndexNumber"] == episode), None)
    if not current:
        return {"episodes": [], "minimum": 0}
    name = str(current.get("Name") or "")
    if multipart(name):
        group = [item for item in selected if same_work(item.get("Name"), name)]
        minimum = 3 if any("(中" in unicodedata.normalize("NFKC", item.get("Name") or "") for item in group) else 2
    else:
        # 特别篇中单独成条目的 OAD，只判断该作品，不判断整部剧所有特别篇。
        group, minimum = [current], 1
    return {"episodes": sorted({item["IndexNumber"] for item in group}), "minimum": minimum}
