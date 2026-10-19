from pathlib import Path
import tempfile
import unittest

from e2e_agent.sample_shop import ShopStore, create_shop


class ShopTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.clock = [100.0]
        self.store = ShopStore(Path(self.temp.name) / "db.sqlite3", clock=lambda: self.clock[0])
        self.store.prepare("first")
        self.store.prepare("other")
        self.app = create_shop(self.store)
        self.client = self.app.test_client()

    def post(self, path, **data):
        return self.client.post("/shop/first/" + path, data=data, follow_redirects=True)

    def issue(self, phone="15500000001"):
        response = self.post("sms", op="send", phone=phone)
        self.assertIn(b"code sent", response.data)
        with self.store.connection() as db:
            row = db.execute("SELECT * FROM challenges ORDER BY rowid DESC").fetchone()
        return dict(row), self.store.test_code(row["id"])

    def verify(self, row, code, phone="15500000001", client=None, ns="first"):
        return (client or self.client).post(f"/shop/{ns}/sms", data={"op": "verify", "phone": phone, "challenge": row["id"], "code": code}, follow_redirects=True)

    def test_registration_login_cart_refresh_logout_and_reset(self):
        self.assertIn(b"Registration successful", self.post("register", username="user", password="TestPass123!").data)
        self.assertEqual(self.store.stats("first")["login_count"], 0)
        self.assertIn(b"Signed in: user", self.post("login", username="user", password="TestPass123!").data)
        self.assertIn(b'data-testid="quantity">1', self.post("add", sku="book").data)
        self.assertIn(b'data-testid="quantity">1', self.client.get("/shop/first/cart").data)
        self.assertEqual(self.store.stats("first")["login_count"], 1)
        self.post("logout")
        self.assertEqual(self.client.get("/shop/first/cart").status_code, 401)
        self.store.prepare("first")
        self.assertIn(b"Registration successful", self.post("register", username="user", password="TestPass123!").data)

    def test_namespaces_isolate_accounts_cart_and_reset(self):
        self.store.account("first", "same")
        self.store.account("other", "same")
        self.post("login", username="same", password="TestPass123!")
        self.post("add", sku="book")
        self.assertEqual(self.client.get("/shop/other/cart").status_code, 401)
        self.store.prepare("other")
        self.assertIn(b'data-testid="quantity">1', self.client.get("/shop/first/cart").data)

    def test_wrong_password_rejected_and_independent_client_unauthenticated(self):
        self.store.account("first", "user")
        self.assertIn(b"Invalid credentials", self.post("login", username="user", password="Wrong123!").data)
        self.assertEqual(self.store.stats("first")["login_count"], 0)
        self.post("login", username="user", password="TestPass123!")
        self.assertEqual(self.app.test_client().get("/shop/first/cart").status_code, 401)

    def test_product_faults_have_known_behavior_and_are_not_exposed(self):
        for fault, expected in (("valid_login_denied", b"Invalid credentials"), ("invalid_password_accepted", b"Signed in: user")):
            self.store.prepare("first", [fault])
            self.store.account("first", "user")
            password = "wrong" if fault == "invalid_password_accepted" else "TestPass123!"
            response = self.post("login", username="user", password=password)
            self.assertIn(expected, response.data)
            self.assertNotIn(fault.encode(), response.data)
        self.store.prepare("first", ["cart_quantity_wrong"])
        self.store.account("first", "user")
        self.post("login", username="user", password="TestPass123!")
        self.assertIn(b'data-testid="quantity">2', self.post("add", sku="book").data)

    def test_sms_single_use_and_one_send_login(self):
        row, code = self.issue()
        self.assertIn(b"Signed in:", self.verify(row, code).data)
        self.assertIn(b"Invalid, expired", self.verify(row, code).data)
        self.assertEqual(self.store.stats("first"), {"login_count": 1, "send_count": 1})

    def test_sms_binding_to_phone_run_owner_and_challenge(self):
        row, code = self.issue()
        for kwargs in ({"phone": "15500000002"}, {"ns": "other"}, {"client": self.app.test_client()}):
            with self.subTest(kwargs=list(kwargs)):
                self.assertIn(b"Invalid, expired", self.verify(row, code, **kwargs).data)
        new, new_code = self.issue()
        self.assertIn(b"Invalid, expired", self.verify(row, code).data)
        self.assertIn(b"Signed in:", self.verify(new, new_code).data)

    def test_sms_expiry_attempt_limit_and_cancel(self):
        row, code = self.issue()
        self.clock[0] += 60
        self.assertIn(b"Invalid, expired", self.verify(row, code).data)
        row, code = self.issue()
        wrong = "000000" if code != "000000" else "111111"
        for _ in range(3):
            self.assertIn(b"Invalid, expired", self.verify(row, wrong).data)
        self.assertIn(b"Invalid, expired", self.verify(row, code).data)
        row, code = self.issue()
        self.post("sms", op="cancel", challenge=row["id"], phone="15500000001")
        self.assertIn(b"Invalid, expired", self.verify(row, code).data)
        self.assertEqual(self.store.stats("first")["login_count"], 0)

    def test_fault_and_reset_controls_are_not_public_http_routes(self):
        self.assertEqual(self.client.post("/reset").status_code, 404)
        self.assertEqual(self.client.get("/test-code").status_code, 404)
        with self.assertRaises(ValueError):
            self.store.prepare("../escape")
        with self.assertRaises(ValueError):
            self.store.prepare("first", ["unknown"])
