"""CT card authorization port + the key delivered at heartbeat.

The vendor doc (``CT_Lie3_KR_partner_api_DML/同行对接使用说明.txt``) verifies the
card at ``117:8004`` with ``product_id=ct_lie3_partner_in``.  These tests pin two
rules learned from the field:

1. the card authorization URL is always on port 8004 (a stale or hand-edited
   config must not move it);
2. the credential the licence server hands out at heartbeat (``tx_key`` /
   ``auto_lie.secret_key``) must actually be written into the service config -
   the packaged key can be revoked server-side, and when it is, the tracking WS
   answers ``auth_denied: unknown_or_deleted_product_key`` and 8765 never listens.
"""

import json
import tempfile
import unittest
from pathlib import Path

import local_lie_service as svc


def make_service_package(workspace: Path) -> Path:
    """A minimal folder shape ``find_service_root`` recognises as the service."""

    root = workspace / "CT_Lie3_KR_partner_api_DML" / "CT_Lie3_KR_partner_api_DML"
    (root / "data").mkdir(parents=True, exist_ok=True)
    (root / "launch_background.vbs").write_text("' test shim\n", encoding="utf-8")
    return root


def write_config(root: Path, **values) -> Path:
    path = root / "data" / "ct_login_config.json"
    path.write_text(json.dumps(values), encoding="utf-8")
    return path


def write_profile(root: Path, ws_key: str) -> None:
    (root / "data" / "build_profile.json").write_text(
        json.dumps({"variant": "partner_api", "ws_product_key": ws_key}),
        encoding="utf-8",
    )


class AuthServerUrlTests(unittest.TestCase):
    def test_configured_host_is_kept_but_the_port_is_pinned(self) -> None:
        self.assertEqual(
            svc._auth_server_url("http://117.50.223.113:8005"),
            "http://117.50.223.113:8004",
        )
        self.assertEqual(
            svc._auth_server_url("http://117.50.223.113:8004"),
            "http://117.50.223.113:8004",
        )
        self.assertEqual(
            svc._auth_server_url("https://117.50.223.113"),
            "https://117.50.223.113:8004",
        )
        self.assertEqual(
            svc._auth_server_url("http://117.50.223.113:8100/v1"),
            "http://117.50.223.113:8004",
        )

    def test_retired_host_is_replaced_by_the_live_endpoint(self) -> None:
        url = svc._auth_server_url("http://222.215.232.230:8004")
        self.assertTrue(url.endswith(":8004"), url)
        self.assertNotIn("222.215.232.230", url)

    def test_empty_value_uses_the_live_endpoint(self) -> None:
        url = svc._auth_server_url("")
        self.assertTrue(url.startswith("http://"), url)
        self.assertTrue(url.endswith(":8004"), url)

    def test_port_constant_is_the_documented_one(self) -> None:
        self.assertEqual(svc.AUTH_SERVER_PORT, 8004)

    def test_normalize_rewrites_both_files_and_keeps_other_keys(self) -> None:
        workspace = Path(tempfile.mkdtemp(prefix="auth-port-"))
        root = make_service_package(workspace)
        credentials = write_config(
            root,
            license_server_url="http://117.50.223.113:8005",
            partner_license_code="CT_IN-TEST-0001-0002-0003",
            lie_product_key="tw-test-00000-000000",
            lie3_backend="roi_track_ws",
            lie_http_ws_fallback_keep=7,
        )
        legacy = root / "login_config.json"
        legacy.write_text(json.dumps({
            "license_code": "",
            "license_server_url": "http://117.50.223.113:8006",
        }), encoding="utf-8")

        self.assertTrue(svc.normalize_auth_server(workspace))

        stored = json.loads(credentials.read_text(encoding="utf-8"))
        self.assertEqual(stored["license_server_url"], "http://117.50.223.113:8004")
        self.assertEqual(stored["partner_license_code"], "CT_IN-TEST-0001-0002-0003")
        self.assertEqual(stored["lie_product_key"], "tw-test-00000-000000")
        self.assertEqual(stored["lie3_backend"], "roi_track_ws")
        self.assertEqual(stored["lie_http_ws_fallback_keep"], 7)
        engine_config = json.loads(legacy.read_text(encoding="utf-8"))
        self.assertEqual(
            engine_config["license_server_url"], "http://117.50.223.113:8004"
        )
        self.assertFalse(svc.normalize_auth_server(workspace))   # idempotent

    def test_write_credentials_pins_the_port(self) -> None:
        workspace = Path(tempfile.mkdtemp(prefix="auth-port-write-"))
        root = make_service_package(workspace)
        credentials = write_config(
            root,
            license_server_url="http://117.50.223.113:8001",   # a WS port, wrongly
            partner_license_code="",
            lie_product_key="",
        )
        svc.write_credentials(workspace, "CT_IN-TEST-0001-0002-0003")
        stored = json.loads(credentials.read_text(encoding="utf-8"))
        self.assertEqual(stored["license_server_url"], "http://117.50.223.113:8004")
        self.assertEqual(stored["partner_license_code"], "CT_IN-TEST-0001-0002-0003")


class CanonicalCardTests(unittest.TestCase):
    """The delivered card must be a clean CT_IN token before it is written."""

    def test_truncated_double_card_is_reduced_to_the_first_token(self) -> None:
        # The live case: the server's 30-character ct_card field cut
        # "CT_IN-0A58-1KLR-0L76-YW1X CT_IN-..." into "... CT_I".
        self.assertEqual(
            svc.canonical_card("CT_IN-0A58-1KLR-0L76-YW1X CT_I"),
            "CT_IN-0A58-1KLR-0L76-YW1X",
        )

    def test_case_and_whitespace_are_normalised(self) -> None:
        self.assertEqual(
            svc.canonical_card(" ct_in-5acl-gsmn-6x46-ef9q  "),
            "CT_IN-5ACL-GSMN-6X46-EF9Q",
        )

    def test_junk_yields_nothing(self) -> None:
        for value in ("", "nonsense", "CT_", "CT_IN-"):
            self.assertEqual(svc.canonical_card(value), "")

    def test_write_credentials_stores_the_clean_card(self) -> None:
        workspace = Path(tempfile.mkdtemp(prefix="card-clean-"))
        root = make_service_package(workspace)
        credentials = write_config(
            root,
            license_server_url="http://117.50.223.113:8004",
            partner_license_code="",
            lie_product_key="tw-test-00000-000000",
        )
        svc.write_credentials(workspace, "CT_IN-0A58-1KLR-0L76-YW1X CT_I")
        stored = json.loads(credentials.read_text(encoding="utf-8"))
        self.assertEqual(
            stored["partner_license_code"], "CT_IN-0A58-1KLR-0L76-YW1X"
        )


class HeartbeatKeyTests(unittest.TestCase):
    """The key gained at heartbeat must reach the service config."""

    def test_delivered_key_replaces_the_packaged_key(self) -> None:
        workspace = Path(tempfile.mkdtemp(prefix="tx-key-"))
        root = make_service_package(workspace)
        write_profile(root, "LIE-PACKAGE-OWN-001")
        credentials = write_config(
            root,
            license_server_url="http://117.50.223.113:8004",
            partner_license_code="CT_IN-TEST-0001-0002-0003",
            lie_product_key="LIE-PACKAGE-OWN-001",
        )
        # This is the field case: the packaged key is in use, so the refresh used
        # to be skipped.  The delivered key must win now.
        self.assertTrue(svc._product_key_writable(root, json.loads(
            credentials.read_text(encoding="utf-8")
        )))
        svc.write_credentials(
            workspace, "CT_IN-TEST-0001-0002-0003", "tw-test-11111-222222",
        )
        stored = json.loads(credentials.read_text(encoding="utf-8"))
        self.assertEqual(stored["lie_product_key"], "tw-test-11111-222222")

    def test_placeholder_is_still_replaced_by_the_package_key(self) -> None:
        # A template key (the vendor's Chinese placeholder) is never written when
        # the package carries a usable key of its own.
        workspace = Path(tempfile.mkdtemp(prefix="tx-key-ph-"))
        root = make_service_package(workspace)
        write_profile(root, "LIE-PACKAGE-OWN-001")
        credentials = write_config(
            root,
            license_server_url="http://117.50.223.113:8004",
            partner_license_code="",
            lie_product_key="LIE-你的坐标云密钥",
        )
        svc.write_credentials(workspace, "CT_IN-TEST-0001-0002-0003")
        stored = json.loads(credentials.read_text(encoding="utf-8"))
        self.assertEqual(stored["lie_product_key"], "LIE-PACKAGE-OWN-001")


if __name__ == "__main__":
    unittest.main()
