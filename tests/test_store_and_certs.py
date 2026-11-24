import ssl
import tempfile
import unittest
from pathlib import Path

from cryptography import x509

from lancall import certs
from lancall.store import Directory, clean_name


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "directory.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_round_trip(self):
        d = Directory(self.path)
        a = d.add("a" * 20, "s" * 40, "Reception")
        b = d.add("b" * 20, "s" * 40, "Workshop", "250")
        d.add_missed(a, {"id": "1", "ext": "250", "name": "Workshop", "ts": 1})
        d.save()
        again = Directory(self.path)
        again.load()
        self.assertEqual((again.find_ext("100").name, again.find_ext("250").name), ("Reception", "Workshop"))
        self.assertEqual(len(again.get(a.id).missed), 1)
        self.assertTrue(again.get(b.id).check_secret("s" * 40))
        self.assertFalse(again.get(b.id).check_secret("t" * 40))
        self.assertNotIn("s" * 40, self.path.read_text(encoding="utf-8"))  # secrets are stored hashed

    def test_extension_rules(self):
        d = Directory(self.path, ext_start=100)
        a = d.add("a" * 20, "s" * 40, "A", "100")
        b = d.add("b" * 20, "s" * 40, "B", "100")  # taken: falls back to the next free one
        self.assertEqual((a.ext, b.ext), ("100", "101"))
        self.assertFalse(d.set_ext(b, "100"))
        self.assertTrue(d.set_ext(b, "55"))
        self.assertIsNone(d.find_ext("101"))
        for bad in ("7", "012", "1234567", "12a", ""):
            self.assertFalse(d.ext_available(bad), bad)

    def test_corrupt_file_is_set_aside_not_overwritten(self):
        self.path.write_text("{ this is not json", encoding="utf-8")
        d = Directory(self.path)
        d.load()
        self.assertEqual(d.phones, {})
        self.assertEqual(len(list(self.path.parent.glob("directory.corrupt-*.json"))), 1)

    def test_names_are_cleaned(self):
        self.assertEqual(clean_name("  Front \t\n Desk\u0000 "), "Front Desk")
        self.assertEqual(clean_name("x" * 100), "x" * 32)
        self.assertEqual(clean_name(None), "")
        self.assertEqual(clean_name("‮​"), "")

    def test_prune_skips_connected_phones(self):
        d = Directory(self.path)
        a, b = d.add("a" * 20, "s" * 40, "A"), d.add("b" * 20, "s" * 40, "B")
        a.last_seen = b.last_seen = 0
        gone = d.prune(30, keep={a.id})
        self.assertEqual([p.id for p in gone], [b.id])
        self.assertEqual(d.prune(0, keep=set()), [])


class CertsTest(unittest.TestCase):
    def test_issue_reuse_and_reissue(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            self.assertTrue(certs.ensure(folder, ["192.168.1.20", "office-pc"], "office-pc"))
            self.assertFalse(certs.ensure(folder, ["192.168.1.20", "office-pc"], "office-pc"))
            ca_before = (folder / "ca.crt").read_bytes()
            self.assertTrue(certs.ensure(folder, ["192.168.1.99"], "office-pc"))  # address changed
            self.assertEqual(ca_before, (folder / "ca.crt").read_bytes())  # phones keep trusting the same CA

            cert = x509.load_pem_x509_certificate((folder / "server.crt").read_bytes())
            san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
            self.assertIn("192.168.1.99", [str(i) for i in san.get_values_for_type(x509.IPAddress)])
            self.assertIn("localhost", san.get_values_for_type(x509.DNSName))
            self.assertLessEqual((cert.not_valid_after_utc - cert.not_valid_before_utc).days, 398)

            # A real TLS stack accepts the pair, and the chain verifies against the CA alone.
            server = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
            server.load_cert_chain(folder / "server.crt", folder / "server.key")
            client = ssl.create_default_context(cafile=str(folder / "ca.crt"))
            incoming, outgoing = ssl.MemoryBIO(), ssl.MemoryBIO()
            c = client.wrap_bio(incoming, outgoing, server_hostname="192.168.1.99")
            s = server.wrap_bio(outgoing, incoming, server_side=True)
            for _ in range(10):
                done = 0
                for side in (c, s):
                    try:
                        side.do_handshake()
                        done += 1
                    except ssl.SSLWantReadError:
                        pass
                if done == 2:
                    break
            self.assertEqual(done, 2)


if __name__ == "__main__":
    unittest.main()
