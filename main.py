import copy
import re
import tempfile
from pathlib import Path
from typing import List, Tuple, Union

import matplotlib.pyplot as plt
import numpy as np
import PyPDF2
import uvicorn
from fastapi import FastAPI, File, UploadFile
from fastapi.responses import FileResponse
from paddleocr import PaddleOCR, PPStructure
from pdf2image import convert_from_bytes, convert_from_path
from PIL import Image, ImageDraw, ImageFont
from pydantic import BaseModel, Field
from tqdm import tqdm
# from transformers import MarianMTModel, MarianTokenizer
from utils import fw_fill

from utils.openai_util import translate
import os, tiktoken
import nltk
nltk.download('punkt')
from nltk.tokenize import sent_tokenize
max_tokens = 1536
enc = tiktoken.encoding_for_model("gpt-3.5-turbo-0301")
def tik(text):
    return len(enc.encode(text))

class InputPdf(BaseModel):
    """Input PDF file."""
    input_pdf: UploadFile = Field(..., title="Input PDF file")


class TranslateApi:
    """Translator API class.

    Attributes
    ----------
    app: FastAPI
        FastAPI instance
    temp_dir: tempfile.TemporaryDirectory
        Temporary directory for storing translated PDF files
    temp_dir_name: Path
        Path to the temporary directory
    font: ImageFont
        Font for drawing text on the image
    layout_model: PPStructure
        Layout model for detecting text blocks
    ocr_model: PaddleOCR
        OCR model for detecting text in the text blocks
    translate_model: MarianMTModel
        Translation model for translating text
    translate_tokenizer: MarianTokenizer
        Tokenizer for the translation model
    """
    DPI = 300
    FONT_SIZE = 32

    def __init__(self):
        self.app = FastAPI()
        self.app.add_api_route(
            "/translate_pdf/",
            self.translate_pdf,
            methods=["POST"],
            response_class=FileResponse,
        )
        self.app.add_api_route(
            "/clear_temp_dir/",
            self.clear_temp_dir,
            methods=["GET"],
        )

        self.__load_models()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.temp_dir_name = Path(self.temp_dir.name)

    def run(self):
        """Run the API server"""
        # uvicorn.run(self.app, host="0.0.0.0", port=8765)
        uvicorn.run(self.app, host="localhost", port=8765)

    async def translate_pdf(self, input_pdf: UploadFile = File(...)) -> FileResponse:
        """API endpoint for translating PDF files.

        Parameters
        ----------
        input_pdf: UploadFile
            Input PDF file

        Returns
        -------
        FileResponse
            Translated PDF file
        """
        # print('?')
        input_pdf_data = await input_pdf.read()
        # print('??:',type(input_pdf_data))
        self._translate_pdf(input_pdf_data, self.temp_dir_name)

        return FileResponse(
            self.temp_dir_name / "translated.pdf", media_type="application/pdf"
        )

    async def clear_temp_dir(self):
        """API endpoint for clearing the temporary directory."""
        self.temp_dir.cleanup()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.temp_dir_name = Path(self.temp_dir.name)
        return {"message": "temp dir cleared"}

    def _translate_pdf(self, pdf_path_or_bytes: Union[Path, bytes], output_dir: Path) -> None:
        """Backend function for translating PDF files.

        Translation is performed in the following steps:
            1. Convert the PDF file to images
            2. Detect text blocks in the images
            3. For each text block, detect text and translate it
            4. Draw the translated text on the image
            5. Save the image as a PDF file
            6. Merge all PDF files into one PDF file

        At 3, this function does not translate the text after
        the references section. Instead, saves the image as it is.

        Parameters
        ----------
        pdf_path_or_bytes: Union[Path, bytes]
            Path to the input PDF file or bytes of the input PDF file
        output_dir: Path
            Path to the output directory
        """
        if isinstance(pdf_path_or_bytes, Path):
            pdf_images = convert_from_path(pdf_path_or_bytes, dpi=self.DPI)#, poppler_path = r"C:\poppler-23.01.0\Library\bin")
        else:
            pdf_images = convert_from_bytes(pdf_path_or_bytes, dpi=self.DPI)#, poppler_path = r"C:\poppler-23.01.0\Library\bin")

        pdf_files = []
        reached_references = False
        for i, image in tqdm(enumerate(pdf_images)):
            output_path = output_dir / f"{i:03}.pdf"
            if not reached_references:
                img, original_img, reached_references = self.__translate_one_page(
                    image=image,
                    reached_references=reached_references,
                )
                fig, ax = plt.subplots(1, 2, figsize=(20, 14))
                ax[0].imshow(original_img)
                ax[1].imshow(img)
                ax[0].axis("off")
                ax[1].axis("off")
                plt.tight_layout()
                plt.savefig(output_path, format="pdf", dpi=self.DPI)
                plt.close(fig)
            else:
                (
                    image.convert("RGB")
                    .resize((int(1400 / image.size[1] * image.size[0]), 1400))
                    .save(output_path, format="pdf")
                )

            pdf_files.append(str(output_path))

        self.__merge_pdfs(pdf_files)

    def __load_models(self):
        """Backend function for loading models.

        Called in the constructor.
        Load the layout model, OCR model, translation model and font.
        """
        self.font = ImageFont.truetype(
            "assets/SourceHanSerif-Light.otf",
            size=self.FONT_SIZE,
        )

        self.layout_model = PPStructure(table=False, ocr=False, lang="en")
        self.ocr_model = PaddleOCR(ocr=True, lang="en", ocr_version="PP-OCRv3")

        # self.translate_model = MarianMTModel.from_pretrained("staka/fugumt-en-ja").to(
        #     "cuda"
        # )
        # self.translate_tokenizer = MarianTokenizer.from_pretrained("staka/fugumt-en-ja")

    def __translate_one_page(
        self,
        image: Image.Image,
        reached_references: bool,
    ) -> Tuple[np.ndarray, np.ndarray, bool]:
        """Translate one page of the PDF file.

        There are some heuristics to clean-up the results of translation:
            1. Remove newlines, tabs, brackets, slashes, and pipes
            2. Reject the result if there are few Japanese characters
            3. Skip the translation if the text block has only one line

        Parameters
        ----------
        image: Image.Image
            Image of the page
        reached_references: bool
            Whether the references section has been reached.

        Returns
        -------
        Tuple[np.ndarray, np.ndarray, bool]
            Translated image, original image,
            and whether the references section has been reached.
        """
        img = np.array(image, dtype=np.uint8)
        original_img = copy.deepcopy(img)
        result = self.layout_model(img)
        for line in result:
            if not line["type"] == "title":
                ocr_results = list(map(lambda x: x[0], self.ocr_model(line["img"])[1]))

                if len(ocr_results) > 1:
                    text = " ".join(ocr_results)
                    text = re.sub(r"\n|\t|\[|\]|\/|\|", " ", text)
                    translated_text = self.__translate(text)

                    # if almost all characters in translated text are not japanese characters, skip
                    if len(
                        re.findall(
                            r"[^\u3040-\u309F\u30A0-\u30FF\u4E00-\u9FFF\u3400-\u4DBF]",
                            translated_text,
                        )
                    ) > 0.8 * len(translated_text):
                        print("skipped")
                        continue

                    # if text is too short, skip
                    if len(translated_text) < 20:
                        print("skipped")
                        continue

                    processed_text = fw_fill(
                        translated_text,
                        width=int(
                            (line["bbox"][2] - line["bbox"][0]) / (self.FONT_SIZE / 2)
                        )
                        - 1,
                    )
                    print(processed_text)

                    new_block = Image.new(
                        "RGB",
                        (
                            line["bbox"][2] - line["bbox"][0],
                            line["bbox"][3] - line["bbox"][1],
                        ),
                        color=(255, 255, 255),
                    )
                    draw = ImageDraw.Draw(new_block)
                    draw.text(
                        (0, 0),
                        text=processed_text,
                        font=self.font,
                        fill=(0, 0, 0),
                    )
                    new_block = np.array(new_block)
                    img[
                        int(line["bbox"][1]) : int(line["bbox"][3]),
                        int(line["bbox"][0]) : int(line["bbox"][2]),
                    ] = new_block
            else:
                try:
                    title = self.ocr_model(line["img"])[1][0][0]
                except IndexError:
                    continue
                if title.lower() == "references" or title.lower() == "reference":
                    reached_references = True
        return img, original_img, reached_references

    def __translate(self, text: str) -> str:
        """Translate text using the translation model.

        If the text is too long, it will be splited with
        the heuristic that each sentence should be within 448 characters.

        Parameters
        ----------
        text: str
            Text to be translated.

        Returns
        -------
        str
            Translated text.
        """
        # print(text)
        # texts = self.__split_text(text, 448)
        # print(texts)

        translated_texts = []
        # for i, t in enumerate(texts):
        #     inputs = self.translate_tokenizer(t, return_tensors="pt").input_ids.to(
        #         "cuda"
        #     )
        #     outputs = self.translate_model.generate(inputs, max_length=512)
        #     res = self.translate_tokenizer.decode(outputs[0], skip_special_tokens=True)
        #     # skip weird translations
        #     if res.startswith("「この版"):
        #         continue
        #     translated_texts.append(res)
        # print(translated_texts)
        # return "".join(translated_texts)
        # for i, t in enumerate(texts):
        #     res = translate(t,debug=True)
        #     translated_texts.append(res)
        sentences = sent_tokenize(text)
        chunks = []
        chunk = ""
        for sentence in sentences:
            print(sentence, tik(chunk), tik(sentence))
            if (tik(chunk)+tik(sentence)) <= max_tokens:
            # if len(chunk.split()) + len(sentence.split()) <= max_tokens:
                chunk += " " + sentence
            else:
                chunks.append(chunk.strip())
                chunk = sentence
        if chunk:
            chunks.append(chunk.strip())
        for c in chunks:
            res = translate(c,debug=True)
            translated_texts.append(res)

        return "".join(translated_texts)

    def __split_text(self, text: str, text_limit: int = 448) -> List[str]:
        """Split text into chunks of sentences within text_limit.

        Parameters
        ----------
        text: str
            Text to be split.
        text_limit: int
            Maximum length of each chunk. Defaults to 448.

        Returns
        -------
        List[str]
            List of text chunks,
            each of which is shorter than text_limit.
        """
        if len(text) < text_limit:
            return [text]

        sentences = text.rstrip().split(". ")
        sentences = [s + ". " for s in sentences[:-1]] + sentences[-1:]
        result = []
        current_text = ""
        for sentence in sentences:
            if len(current_text) + len(sentence) < text_limit:
                current_text += sentence
            else:
                if current_text:
                    result.append(current_text)
                while len(sentence) >= text_limit:
                    # better to look for a white space at least?
                    result.append(sentence[:text_limit - 1])
                    sentence = sentence[text_limit - 1:].lstrip()
                current_text = sentence
        if current_text:
            result.append(current_text)
        return result

    def __merge_pdfs(self, pdf_files: List[str]) -> None:
        """Merge translated PDF files into one file.

        Merged file will be stored in the temp directory
        as "translated.pdf".

        Parameters
        ----------
        pdf_files: List[str]
            List of paths to translated PDF files stored in
            the temp directory.
        """
        pdf_merger = PyPDF2.PdfMerger()

        for pdf_file in sorted(pdf_files):
            pdf_merger.append(pdf_file)
        pdf_merger.write(self.temp_dir_name / "translated.pdf")


if __name__ == "__main__":
    translate_api = TranslateApi()
    translate_api.run()
