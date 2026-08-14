import unittest

import duckdb

from app.crm import init_crm, init_crm_documents, save_offer_document


class CrmDocumentTest(unittest.TestCase):
    def test_document_migration_can_run_independently_and_repeatedly(self):
        conn = duckdb.connect(":memory:")
        init_crm_documents(conn)
        init_crm_documents(conn)
        self.assertEqual(
            conn.execute(
                "SELECT count(*) FROM information_schema.tables "
                "WHERE table_name='crm_documents'"
            ).fetchone()[0],
            1,
        )
        conn.close()

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
