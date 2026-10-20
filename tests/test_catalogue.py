import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import create_app


class CatalogueTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = create_app({
            "TESTING": True,
            "SECRET_KEY": "catalogue-test-secret",
            "DATABASE": str(Path(self.tmp.name) / "test.db"),
        })
        self.client = self.app.test_client()

        self.client.get("/register")
        with self.client.session_transaction() as session:
            csrf = session["csrf"]

        response = self.client.post("/register", data={
            "email": "catalogue-test@example.com",
            "password": "TestPassword123!",
            "csrf_token": csrf,
        })
        assert response.status_code == 302, (
            f"Registration failed: {response.status_code}"
        )

        self.client.get("/login")
        with self.client.session_transaction() as session:
            csrf = session["csrf"]

        response = self.client.post("/login", data={
            "email": "catalogue-test@example.com",
            "password": "TestPassword123!",
            "csrf_token": csrf,
        })
        assert response.status_code == 302, (
            f"Login failed: {response.status_code}"
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_catalogue_requires_login(self):
        client = self.app.test_client()
        response = client.get("/catalogue")
        self.assertEqual(response.status_code, 302)

    @patch("five_sim.get_countries", return_value={
        "nigeria": {"name": "Nigeria"},
    })
    @patch("five_sim.get_products", return_value={
        "whatsapp": {"Category": "activation", "Qty": 25, "Price": 0.28},
        "telegram": {"Category": "activation", "Qty": 10, "Price": 0.32},
    })
    def test_catalogue_displays_services(self, mock_products, mock_countries):
        response = self.client.get("/catalogue")
        self.assertEqual(response.status_code, 200)
        page = response.get_data(as_text=True).lower()
        self.assertIn("whatsapp", page)
        self.assertIn("telegram", page)
        self.assertIn("0.28", page)

    @patch("five_sim.get_countries", return_value={
        "nigeria": {"name": "Nigeria"},
    })
    @patch("five_sim.get_products", return_value={
        "whatsapp": {"Category": "activation", "Qty": 25, "Price": 0.28},
        "telegram": {"Category": "activation", "Qty": 10, "Price": 0.32},
    })
    def test_search_filters_services(self, mock_products, mock_countries):
        response = self.client.get("/catalogue?country=nigeria&q=telegram")
        self.assertEqual(response.status_code, 200)
        page = response.get_data(as_text=True).lower()
        self.assertIn("telegram", page)
        self.assertNotIn("<td>whatsapp</td>", page)


if __name__ == "__main__":
    unittest.main()
