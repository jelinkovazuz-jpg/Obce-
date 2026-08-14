import unittest

import duckdb

from app.crm import init_crm, save_offer_document


class CrmDocumentTest(unittest.TestCase):
    def test_offer_is_saved_once_and_new_export_replaces_it(self):
        conn = duckdb.connect(":memory:")
        init_crm(conn)
        first_id, replaced = save_offer_document(
            conn, 123, "quote-1", "nabidka.pdf", b"%PDF-first", "tester"
        )
        self.assertFalse(replaced)
        second_id, replaced = save_offer_document(
            conn, 123, "quote-1", "nabidka.pdf", b"%PDF-second", "tester"
        )
        self.assertTrue(replaced)
        self.assertEqual(first_id, second_id)
        rows = conn.execute(
            "SELECT file_data FROM crm_documents WHERE quote_id='quote-1'"
        ).fetchall()
        self.assertEqual(rows, [(b"%PDF-second",)])
        conn.close()


if __name__ == "__main__":
    unittest.main()
