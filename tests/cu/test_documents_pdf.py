"""文本型 PDF 提取与加密/页数边界。"""
import io
import unittest

from pypdf import PdfWriter
from pypdf.generic import (DictionaryObject, NameObject, DecodedStreamObject)

from companion.files import extract
from companion.security import Denied


def text_pdf(value="Hello PDF"):
    writer = PdfWriter()
    page = writer.add_blank_page(width=300, height=200)
    font = DictionaryObject({
        NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type1"),
        NameObject("/BaseFont"): NameObject("/Helvetica"),
    })
    page[NameObject("/Resources")] = DictionaryObject({
        NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})
    })
    stream = DecodedStreamObject()
    stream.set_data(f"BT /F1 12 Tf 72 120 Td ({value}) Tj ET".encode())
    page[NameObject("/Contents")] = writer._add_object(stream)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


class PdfExtractionTest(unittest.TestCase):
    def test_text_pdf_is_extracted(self):
        self.assertIn("Hello PDF", extract(".pdf", text_pdf()))

    def test_encrypted_pdf_is_rejected(self):
        writer = PdfWriter()
        writer.add_blank_page(width=200, height=200)
        writer.encrypt("secret")
        out = io.BytesIO()
        writer.write(out)
        with self.assertRaises(Denied) as raised:
            extract(".pdf", out.getvalue())
        self.assertEqual(raised.exception.args[0], "encrypted_document")

    def test_page_limit_is_enforced(self):
        writer = PdfWriter()
        for _ in range(101):
            writer.add_blank_page(width=100, height=100)
        out = io.BytesIO()
        writer.write(out)
        with self.assertRaises(Denied) as raised:
            extract(".pdf", out.getvalue())
        self.assertEqual(raised.exception.args[0], "document_page_limit")

    def test_unknown_format_is_rejected(self):
        with self.assertRaises(Denied) as raised:
            extract(".exe", b"MZ")
        self.assertEqual(raised.exception.args[0], "unsupported_document")


if __name__ == "__main__":
    unittest.main()
