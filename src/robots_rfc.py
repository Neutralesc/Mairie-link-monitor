"""Analyseur de robots.txt conforme à la RFC 9309 (stdlib uniquement).

Règles appliquées :
  * groupes : une ou plusieurs lignes User-agent consécutives ouvrent un groupe ; les directives
    Allow / Disallow / Crawl-delay qui suivent s'y rattachent ; Sitemap est global ;
  * groupe retenu : celui dont un User-agent est la sous-chaîne (insensible à la casse) la plus longue
    du nom de l'agent ; à défaut le groupe « * » ; à défaut tout est autorisé ;
  * règles : le motif le plus long l'emporte ; à égalité, Allow l'emporte ; motifs avec « * » et « $ » final ;
  * comparaison après urllib.parse.unquote des deux côtés.
"""

from __future__ import annotations

import math
import re
from typing import Iterable
from urllib.parse import unquote, urlsplit

_ROBOTS_PATH = "/robots.txt"


class _Group:
    __slots__ = ("rules", "crawl_delay")

    def __init__(self) -> None:
        self.rules: list[tuple[str, bool, re.Pattern[str]]] = []   # (motif, allow, regex compilée)
        self.crawl_delay: float | None = None


def _compile(pattern: str) -> re.Pattern[str]:
    anchored = pattern.endswith("$")
    body = pattern[:-1] if anchored else pattern
    regex = ".*".join(re.escape(part) for part in body.split("*"))
    return re.compile("^" + regex + (r"\Z" if anchored else ""))


class RobotsRules:
    def __init__(self) -> None:
        self._groups: dict[str, _Group] = {}
        self._sitemaps: list[str] = []

    def parse(self, lines: Iterable[str]) -> None:
        self._groups = {}
        self._sitemaps = []
        current: _Group | None = None
        last_was_agent = False                  # vrai tant qu'aucune règle n'a suivi les User-agent courants
        first = True
        for raw in lines:
            line = raw
            if first:
                line = line.lstrip("﻿")
                first = False
            line = line.split("#", 1)[0].strip()
            if not line or ":" not in line:
                continue
            key, _, value = line.partition(":")
            key = key.strip().lower()
            value = value.strip()
            if key == "user-agent":
                if not last_was_agent:
                    current = _Group()                       # nouveau groupe
                agent = value.lower()
                if agent:
                    current = self._groups.setdefault(agent, current)   # fusion des groupes homonymes
                last_was_agent = True
            elif key == "sitemap":
                if value:
                    self._sitemaps.append(value)
            elif key in ("allow", "disallow", "crawl-delay"):
                last_was_agent = False
                if current is None:
                    continue                                 # directive hors groupe : ignorée
                if key == "crawl-delay":
                    if current.crawl_delay is None:
                        try:
                            delay = float(value)
                        except ValueError:
                            delay = None
                        if delay is not None and math.isfinite(delay) and delay >= 0:
                            current.crawl_delay = delay
                    continue
                if not value:
                    continue                                 # « Disallow: » vide : ne rien interdire
                pattern = unquote(value)
                current.rules.append((pattern, key == "allow", _compile(pattern)))
            # autres directives : ignorées sans fermer le groupe courant

    def _group_for(self, useragent: str) -> _Group | None:
        ua = useragent.lower()
        best: str | None = None
        for agent in self._groups:
            if agent == "*" or agent not in ua:
                continue
            if best is None or len(agent) > len(best):
                best = agent
        if best is not None:
            return self._groups[best]
        return self._groups.get("*")

    def can_fetch(self, useragent: str, url: str) -> bool:
        parts = urlsplit(url)
        path = parts.path or "/"
        if path == _ROBOTS_PATH:
            return True
        group = self._group_for(useragent)
        if group is None:
            return True
        target = unquote(path + ("?" + parts.query if parts.query else ""))
        best: tuple[int, bool] | None = None
        for pattern, allow, regex in group.rules:
            if regex.match(target):
                key = (len(pattern), allow)
                if best is None or key > best:
                    best = key
        return True if best is None else best[1]

    def crawl_delay(self, useragent: str) -> float | None:
        group = self._group_for(useragent)
        return None if group is None else group.crawl_delay

    def site_maps(self) -> list[str] | None:
        return list(self._sitemaps) if self._sitemaps else None
