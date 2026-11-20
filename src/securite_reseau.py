"""Protection anti-SSRF : l'hôte vers lequel on se connecte doit être une adresse publique.

Le crawler suit les redirections de sites tiers ; sans ce contrôle, un site compromis pourrait le faire contacter
localhost, le réseau privé ou les métadonnées d'un hébergeur (169.254.169.254). Ce module est sans dépendance (stdlib).
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
import time
from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)

RAISON_NON_PUBLIQUE = "adresse_non_publique"
SUFFIXES_INTERDITS = (".localhost", ".local", ".internal")
CACHE_DUREE_S = 300.0

_NAT64 = ipaddress.ip_network("64:ff9b::/96")
_CGNAT = ipaddress.ip_network("100.64.0.0/10")
_CACHE: dict[str, tuple[float, str | None]] = {}     # hôte -> (instant de mise en cache, verdict)

Resolveur = Callable[[str], Awaitable[list[str]]]


def ip_autorisee(ip: str) -> bool:
    """True seulement pour une adresse unicast publique routable."""
    try:
        adresse = ipaddress.ip_address(ip.split("%", 1)[0].strip("[]"))
    except ValueError:
        return False
    if adresse.version == 6:       # IPv4 embarquée (mappée, 6to4, Teredo, NAT64) : contrôlée comme une IPv4
        if adresse.ipv4_mapped is not None:
            adresse = adresse.ipv4_mapped
        elif adresse.sixtofour is not None:
            adresse = adresse.sixtofour
        elif adresse.teredo is not None:      # (serveur, client) : les deux doivent être publics
            serveur, client = adresse.teredo
            return ip_autorisee(str(serveur)) and ip_autorisee(str(client))
        elif adresse in _NAT64:
            adresse = ipaddress.IPv4Address(int(adresse) & 0xFFFFFFFF)
    if adresse.version == 4 and adresse in _CGNAT:
        return False
    return (adresse.is_global and not adresse.is_multicast and not adresse.is_reserved
            and not adresse.is_loopback and not adresse.is_link_local and not adresse.is_private
            and not adresse.is_unspecified)


async def _resoudre_defaut(hote: str) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(hote, None, type=socket.SOCK_STREAM)
    return [info[4][0] for info in infos]


async def verifier_hote(hote: str, resoudre: Resolveur | None = None) -> str | None:
    """None si l'hôte peut être contacté, sinon une raison courte (RAISON_NON_PUBLIQUE).

    Règles : localhost et suffixes locaux refusés ; une adresse IP littérale doit être publique ; un nom doit se
    résoudre (sinon None : l'erreur DNS est traitée par le flux normal) et TOUTES ses adresses doivent être publiques.
    Le verdict est mis en cache pendant CACHE_DUREE_S secondes par hôte."""
    hote = (hote or "").strip().strip("[]").rstrip(".").lower()
    if not hote:
        return RAISON_NON_PUBLIQUE
    try:
        ipaddress.ip_address(hote.split("%", 1)[0])
        return None if ip_autorisee(hote) else RAISON_NON_PUBLIQUE
    except ValueError:
        pass
    if hote == "localhost" or hote.endswith(SUFFIXES_INTERDITS):
        return RAISON_NON_PUBLIQUE
    if hote.rsplit(".", 1)[-1].isdigit():         # TLD tout numérique : pas un nom public (ex. www.169.254.169.254, 127.1)
        return RAISON_NON_PUBLIQUE
    maintenant = time.monotonic()
    cache = _CACHE.get(hote)
    if cache is not None and maintenant - cache[0] < CACHE_DUREE_S:
        return cache[1]
    try:
        adresses = await (resoudre or _resoudre_defaut)(hote)
    except Exception:  # noqa: BLE001 - échec de résolution : pas un refus, le flux normal le signalera
        return None
    if not adresses:
        return None
    verdict = None if all(ip_autorisee(a) for a in adresses) else RAISON_NON_PUBLIQUE
    if verdict is not None:
        logger.warning("%s refusé : résolu vers %s", hote, [a for a in adresses if not ip_autorisee(a)])
    _CACHE[hote] = (maintenant, verdict)
    return verdict


def vider_cache() -> None:
    _CACHE.clear()
