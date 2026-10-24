"""Shared offline fixtures."""
from __future__ import annotations

import pytest


@pytest.fixture
def fake_registry(tmp_path, monkeypatch):
    import gzip as _gz
    from flightplanner import registry as R
    with _gz.open(tmp_path / "faa_aircraft.csv.gz", "wt") as f:
        f.write("n_number,year,model_code,engine_code\n"
                "1014F,1979,2072738,17026\n101SP,1986,2073020,41525\n2RX,2004,1150020,41540\n77AB,,9999999,\n")
    (tmp_path / "faa_models.csv").write_text(
        "code,mfr,model,type_acft,build_cert,engines,seats\n"
        "2072738,CESSNA,172N,4,0,01,004\n2073020,CESSNA,182R,4,0,01,004\n"
        "1150020,CIRRUS DESIGN CORP,SR22,4,0,01,004\n9999999,BEECH,58,5,0,02,006\n")
    (tmp_path / "faa_engines.csv").write_text(
        "code,mfr,model,hp\n17026,LYCOMING,0-320 SERIES,00180\n41525,CONT MOTOR,O-470 SERIES,00230\n"
        "41540,CONT MOTOR,IO-550-N,00310\n")
    monkeypatch.setattr(R, "DATA_DIR", tmp_path)
    for fn in (R._models, R._engines, R.lookup):
        fn.cache_clear()
    yield R
    for fn in (R._models, R._engines, R.lookup):
        fn.cache_clear()
