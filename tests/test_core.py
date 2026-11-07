"""Testes unitários do núcleo (puros e de banco). Não requer Discord conectado."""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

# Força o core a usar um banco temporário.
os.environ["OWNER_DISCORD_ID"] = "123"
REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from core import services  # noqa: E402
from core.common import money  # noqa: E402
from core.database import get_db, migrate  # noqa: E402


def test_money_format():
    assert money(0) == "R$ 0,00"
    assert money(100) == "R$ 1,00"
    assert money(125) == "R$ 1,25"
    assert money(2000) == "R$ 20,00"


def test_billing_step_never_exceeds_price():
    price = 200  # cents
    total_min = 60
    milli, billed, charged = 0, 0, 0
    total_charged = 0
    for _ in range(total_min + 10):  # minutos a mais não devem cobrar além do teto
        milli, billed, charged = services.billing_step(price, total_min, milli, billed, 1)
        total_charged += charged
    assert billed == price, f"billed={billed} deveria ser {price}"
    assert total_charged == price
    assert 0 <= milli < 1000


def test_billing_step_zero_minutes():
    milli, billed, charged = services.billing_step(100, 0, 0, 0, 5)
    assert charged == 0
    assert billed == 0


def test_remaining_credit_minutes():
    # 200 cents / 60 min -> ~3.333 cents/min. 100 cents compram ~30 min.
    m = services.remaining_credit_minutes(100, 200, 60)
    assert 29 < m < 31


def test_migrate_idempotent_and_seeds(tmp_path, monkeypatch):
    import core.config as cfg
    db_file = tmp_path / "test.db"
    monkeypatch.setattr(cfg, "DB_PATH", db_file)
    monkeypatch.setattr(cfg, "BACKUP_DIR", tmp_path / "backups")
    migrate()
    migrate()  # duas vezes: idempotente
    with get_db() as con:
        durs = services.list_durations(con)
        assert len(durs) == 5
        assert services.price_for_hours(con, 24) == 200
        assert services.max_sessions_per_user(con) == 2
        assert services.maintenance_on(con) is False
        # colunas novas existem
        cols = {r["name"] for r in con.execute("PRAGMA table_info(users)").fetchall()}
        for c in ("nickname", "status", "suspended_reason", "created_at", "last_seen_at"):
            assert c in cols
        cols_s = {r["name"] for r in con.execute("PRAGMA table_info(sessions)").fetchall()}
        for c in ("price_cents", "billed_cents", "billed_millicents", "last_billed_at",
                  "warned_expiry", "warned_low_credit", "downtime_seconds", "renewals"):
            assert c in cols_s


def test_adjust_credits_logs_balance(tmp_path, monkeypatch):
    import core.config as cfg
    monkeypatch.setattr(cfg, "DB_PATH", tmp_path / "t2.db")
    monkeypatch.setattr(cfg, "BACKUP_DIR", tmp_path / "backups")
    migrate()
    with get_db() as con:
        bal = services.adjust_credits(con, 999, 500, actor_id=123, log_type="add", reason="teste")
        assert bal == 500
        bal = services.adjust_credits(con, 999, -200, actor_id=123, log_type="consume", reason="sessao")
        assert bal == 300
        logs = con.execute("SELECT * FROM credit_logs WHERE user_id=999 ORDER BY id").fetchall()
        assert len(logs) == 2
        assert logs[-1]["balance_after"] == 300
        assert logs[-1]["type"] == "consume"


if __name__ == "__main__":
    # Rodar sem pytest: chamar cada teste com tmp_path manual.
    import traceback
    failures = 0
    test_money_format()
    test_billing_step_never_exceeds_price()
    test_billing_step_zero_minutes()
    test_remaining_credit_minutes()
    with tempfile.TemporaryDirectory() as td:

        class MP:
            def __init__(self): self._saved = {}
            def setattr(self, mod, name, val):
                self._saved[(mod, name)] = getattr(mod, name)
                setattr(mod, name, val)
        mp = MP()
        try:
            test_migrate_idempotent_and_seeds(Path(td), mp)
            test_adjust_credits_logs_balance(Path(td), mp)
        except Exception:
            traceback.print_exc()
            failures += 1
    if failures:
        print(f"FALHAS: {failures}")
        sys.exit(1)
    print("Todos os testes do núcleo passaram.")
