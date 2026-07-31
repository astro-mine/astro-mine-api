"""Shared fixtures — the lunar polar water-ice anchor scenario (studio.md §12)."""

from __future__ import annotations

import pytest
from astro_mine.core.objective import ObjectiveDocument
from astro_mine.core.units.model import PlanetaryCRS
from astro_mine.studio.intent import MetricVocabulary
from astro_mine.studio.intent.forms import build_objective
from astro_mine.studio.models import (
    AssetSelection,
    DesignCandidate,
    GeoRegion,
    HardConstraint,
    IntentDraft,
    TargetProduct,
)
from astro_mine.studio.orchestrate import SiblingClients, local_clients


@pytest.fixture
def moon_crs() -> PlanetaryCRS:
    return PlanetaryCRS(body="MOON", body_fixed_frame="MOON_ME", reference_radius_m=1737400.0)


@pytest.fixture
def lunar_draft(moon_crs: PlanetaryCRS) -> IntentDraft:
    return IntentDraft(
        id="lunar-ice",
        name="Lunar polar water-ice prospecting",
        author="designer@astro-mine",
        region=GeoRegion(name="Shackleton rim", crs=moon_crs),
        scenario_ref="sha256:anchor",
        products=[
            TargetProduct(
                criterion_id="water",
                metric="water_production_rate",
                unit="kg/day",
                target=40.0,
                tolerance=60.0,
                rate_window_s=86400.0,
                weight=1.0,
            )
        ],
        constraints=[
            HardConstraint(criterion_id="power", metric="power_margin", unit="W", threshold=0.0)
        ],
        inventory=[AssetSelection(sadf_ref="sha256:rover", count=4)],
    )


@pytest.fixture
def objective_doc(lunar_draft: IntentDraft) -> ObjectiveDocument:
    return build_objective(lunar_draft)


@pytest.fixture
def vocabulary() -> MetricVocabulary:
    return MetricVocabulary(metrics={"water_production_rate": "", "power_margin": ""})


@pytest.fixture
def clients() -> SiblingClients:
    return local_clients()


@pytest.fixture
def candidate() -> DesignCandidate:
    return DesignCandidate(id="cand-a", swarm=[AssetSelection(sadf_ref="sha256:rover", count=4)])
