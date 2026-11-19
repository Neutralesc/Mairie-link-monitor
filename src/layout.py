"""Arborescence des fichiers par département (data/<dep>/, reports/<dep>/, resultats/)."""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


def normalize_dept(code: str) -> str:
    """'1' -> '01', '60' -> '60', '2a' -> '2A', '971' -> '971'."""
    c = str(code).strip().upper()
    return c.zfill(2) if re.fullmatch(r"\d{1,2}", c) else c


@dataclass(frozen=True)
class DeptPaths:
    root: Path
    dept: str

    @property
    def data_dir(self) -> Path:
        return self.root / "data" / self.dept

    @property
    def reports_dir(self) -> Path:
        return self.root / "reports" / self.dept

    @property
    def results_dir(self) -> Path:
        return self.root / "resultats"

    # données brutes
    @property
    def cog_all_types(self) -> Path: return self.data_dir / "cog_all_types.csv"
    @property
    def cog_communes(self) -> Path: return self.data_dir / "cog_communes_com.csv"
    @property
    def dila_records(self) -> Path: return self.data_dir / "dila_records.csv"
    # lot 1
    @property
    def inventory_communes(self) -> Path: return self.reports_dir / "inventory_communes.csv"
    @property
    def inventory_urls(self) -> Path: return self.reports_dir / "inventory_urls.csv"
    @property
    def inventory_orphans(self) -> Path: return self.reports_dir / "inventory_orphans.csv"
    @property
    def inventory_summary(self) -> Path: return self.reports_dir / "inventory_summary.json"
    # lot 2
    @property
    def url_checks(self) -> Path: return self.reports_dir / "url_checks.csv"
    @property
    def url_checks_summary(self) -> Path: return self.reports_dir / "url_checks_summary.json"
    # découverte de sites (communes sans site référencé) et couverture
    @property
    def discovered(self) -> Path: return self.reports_dir / "sites_devines.csv"
    @property
    def coverage(self) -> Path: return self.reports_dir / "couverture.csv"
    # plateformes d'information communale (PanneauPocket)
    @property
    def platform_pages(self) -> Path: return self.reports_dir / "plateformes.csv"
    @property
    def platform_links(self) -> Path: return self.reports_dir / "liens_plateformes.csv"
    # exploration
    @property
    def crawl_sites(self) -> Path: return self.reports_dir / "crawl_sites.csv"
    @property
    def links_found(self) -> Path: return self.reports_dir / "links_found.csv"
    @property
    def crawl_summary(self) -> Path: return self.reports_dir / "crawl_summary.json"
    # résultat minimal
    @property
    def result_csv(self) -> Path: return self.results_dir / f"liens_{self.dept}.csv"


# anciens noms (version Oise seule) -> nouveaux noms, utilisés par `migrate-legacy`
def legacy_map(p: DeptPaths, source: Path | None = None) -> list[tuple[Path, Path]]:
    """Paires (ancien fichier dans `source`, nouveau fichier dans p.root). `source` = ancien dossier de projet."""
    src = source or p.root
    d, r = src / "data", src / "reports"
    return [
        (d / "v_commune_2026.csv", p.root / "data" / "v_commune_2026.csv"),
        (d / "cog_oise_all_types.csv", p.cog_all_types),
        (d / "cog_oise_communes_com.csv", p.cog_communes),
        (d / "dila_oise_all_records.csv", p.dila_records),
        (r / "inventory_oise.csv", p.inventory_communes),
        (r / "inventory_urls_oise.csv", p.inventory_urls),
        (r / "dila_mairies_hors_cog.csv", p.inventory_orphans),
        (r / "inventory_summary.json", p.inventory_summary),
        (r / "url_checks_oise.csv", p.url_checks),
        (r / "url_checks_summary.json", p.url_checks_summary),
        (r / "crawl_sites_oise.csv", p.crawl_sites),
        (r / "links_found_oise.csv", p.links_found),
        (r / "crawl_summary.json", p.crawl_summary),
    ]
