import json
import os
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

import five_sim


class FiveSimTests(unittest.TestCase):

    def test_configured_without_key(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(five_sim.configured())

    def test_configured_with_key(self):
        with patch.dict(os.environ, {"FIVE_SIM_API_KEY": "test-key"}):
            self.assertTrue(five_sim.configured())

    def test_profile_requires_key(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(five_sim.FiveSimError):
                five_sim.get_profile()

    @patch("five_sim.urllib.request.urlopen")
    def test_get_countries(self, mock_urlopen):
        class Response:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def read(self):
                return json.dumps({"nigeria": {}}).encode()

        mock_urlopen.return_value = Response()

        result = five_sim.get_countries()

        self.assertIn("nigeria", result)
        req = mock_urlopen.call_args.args[0]
        self.assertEqual(req.get_header("User-agent"), "NOVA-NUMBER-HUB/1.0")

    @patch("five_sim.urllib.request.urlopen")
    def test_profile_uses_bearer_auth(self, mock_urlopen):
        class Response:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def read(self):
                return b'{"id": 123}'

        mock_urlopen.return_value = Response()

        with patch.dict(os.environ, {"FIVE_SIM_API_KEY": "test-key"}):
            result = five_sim.get_profile()

        self.assertEqual(result["id"], 123)
        req = mock_urlopen.call_args.args[0]
        self.assertEqual(req.get_header("Authorization"), "Bearer test-key")

    @patch("five_sim.urllib.request.urlopen")
    def test_http_403_becomes_safe_error(self, mock_urlopen):
        mock_urlopen.side_effect = HTTPError(
            "https://5sim.net/v1/guest/countries",
            403,
            "Forbidden",
            {},
            None,
        )

        with self.assertRaises(five_sim.FiveSimError):
            five_sim.get_countries()


if __name__ == "__main__":
    unittest.main()
