import sys
import os
from PyQt5.uic import loadUi
from PyQt5 import QtGui
from PyQt5.QtWidgets import QDialog, QApplication, QFileDialog, QMessageBox
from PyQt5.QtCore import pyqtSignal, QObject, QThread
import pdfplumber
import re
from pandas import DataFrame, Series # Added Series for pd.to_numeric
import pandas as pd # For pd.to_numeric
from datetime import datetime

# Determine basedir correctly for bundled applications (e.g., PyInstaller)
if getattr(sys, 'frozen', False):
    basedir = sys._MEIPASS #pylint: disable=no-member,protected-access
else:
    basedir = os.path.dirname(__file__)

class Worker(QObject):
    error_occurred = pyqtSignal(str)
    processing_done = pyqtSignal(str)

    def __init__(self, file_path, doc_pwd):
        super().__init__()
        self.file_path = file_path
        self.doc_pwd = doc_pwd

        # Pre-compiled regex patterns
        self.folio_pat = re.compile(r"^Folio No:\s*(\d+\s*/\s*\d+)", re.IGNORECASE)
        self.trans_details_pat = re.compile(
            r"^\s*(\d{2}-[A-Za-z]{3}-\d{4})"  # G1: Date
            r"\s+(.+?)"                      # G2: Description (non-greedy)
            # Lookahead for the start of numeric fields to anchor description.
            # Assumes at least 3 numeric fields follow (Amount, Units, Price/Balance)
            r"(?=\s+[\d(][\d.,()]*\s+[\d(][\d.,()]*\s+[\d(][\d.,()]*)"
            r"\s+([\d.,()]+)"               # G3: Amount
            r"\s+([\d.,()]+)"               # G4: Units
            r"\s+([\d.,()]+)"               # G5: NAV/Price
            r"\s+([\d.,()]+)\s*$"           # G6: Unit Balance (often to end of line)
        )
        # Regexes for extracting details from the header block
        self.isin_pat_detail = re.compile(r'ISIN:\s*([A-Z0-9]{12})', re.IGNORECASE)
        self.advisor_pat_detail = re.compile(r'Advisor:\s*([^\n]+)', re.IGNORECASE) # Takes rest of the line
        self.registrar_pat_detail = re.compile(r'Registrar\s*:\s*([^\n]+)', re.IGNORECASE) # Takes rest of the line


    def run(self):
        """Entry point for the worker thread."""
        try:
            self.process_pdf_file()
        except Exception as e:
            # import traceback # Uncomment for debugging
            # traceback.print_exc() # Uncomment for debugging
            self.error_occurred.emit(f"An unexpected error occurred in the worker: {str(e)}")

    def process_pdf_file(self):
        """Handles PDF opening, text extraction, and calls data parsing."""
        if not self.file_path:
            raise ValueError("No PDF file selected.") # Should be caught by UI first

        final_text = ""
        try:
            with pdfplumber.open(self.file_path, password=self.doc_pwd) as pdf:
                if not pdf.pages:
                    raise ValueError("The PDF file contains no pages.")
                
                page_texts = []
                for i, page in enumerate(pdf.pages):
                    text = page.extract_text_simple() # Tries to maintain reading order
                    if text:
                        page_texts.append(text)
                    # else:
                    #     print(f"Warning: No text extracted from page {i+1}")
                final_text = "\n".join(page_texts)

            if not final_text.strip():
                raise ValueError("No text could be extracted from the PDF. It might be an image-based PDF, contain no text, or use an unsupported encoding.")

            self._extract_data_from_text(final_text)

        except pdfplumber.pdfminer.pdfdocument.PDFPasswordIncorrect:
            if self.doc_pwd:
                raise ValueError("Incorrect password provided for the PDF file.")
            else:
                raise ValueError("The PDF file is encrypted. Please check the 'Password Protected' box and enter the password.")
        except pdfplumber.exceptions.PDFSyntaxError:
             raise ValueError("The PDF file appears to be malformed or corrupted.")
        except Exception as e:
            # import traceback # Uncomment for debugging
            # traceback.print_exc() # Uncomment for debugging
            raise ValueError(f"An error occurred while opening or reading the PDF: {e}")

    def _extract_funds_details(self, text_block):
        """Extracts fund name, ISIN, Advisor, Registrar from a block of text."""
        fund_name, isin, advisor, registrar = None, None, None, None

        isin_match = self.isin_pat_detail.search(text_block)
        if isin_match: isin = isin_match.group(1).strip()

        advisor_match = self.advisor_pat_detail.search(text_block)
        if advisor_match: advisor = advisor_match.group(1).strip()

        registrar_match = self.registrar_pat_detail.search(text_block)
        if registrar_match: registrar = registrar_match.group(1).strip()
        
        potential_fund_name = None
        lines = text_block.splitlines()
        
        # Heuristic for fund name:
        # 1. Look for a line containing "ISIN:" and extract text before it.
        # 2. If not found, look at the first few prominent lines that are not advisor/registrar.
        for line_content in lines:
            line = line_content.strip()
            if not line: continue

            if "ISIN:" in line.upper():
                match = re.search(r'^(.*?)(?:\s*-\s*ISIN:|\s*ISIN:)', line, re.IGNORECASE)
                if match:
                    temp_name = match.group(1).strip()
                    if temp_name: # Ensure it's not empty
                        potential_fund_name = temp_name
                        break # Found a good candidate

        if not potential_fund_name: # Fallback if ISIN line didn't yield fund name
            for line_content in lines[:3]: # Check first 3 lines
                line = line_content.strip()
                if not line or self.advisor_pat_detail.search(line) or self.registrar_pat_detail.search(line) or "NOMINEE" in line.upper():
                    continue
                # If the line contains typical fund keywords, consider it.
                if any(kw in line.upper() for kw in ["FUND", "PLAN", "SCHEME", "GROWTH", "DIRECT", "OPTION"]):
                    potential_fund_name = line
                    break # Take the first plausible line

        if potential_fund_name:
            fund_name = re.sub(r'(\s*\(formerly.*?\)|s*\(erstwhile.*?\))', '', potential_fund_name, flags=re.IGNORECASE).strip()
            fund_name = re.sub(r'\s*-\s*$', '', fund_name).strip() # Remove trailing " - "

        return fund_name, isin, advisor, registrar

    def _extract_data_from_text(self, doc_txt):
        """Parses the extracted text to find folio, fund details, and transactions."""
        line_itms = []
        current_folio = ""
        current_fund_details = {} # Stores {fund_name, isin, advisor, registrar}

        lines = doc_txt.splitlines()
        i = 0
        while i < len(lines):
            line = lines[i].strip()
            if not line: # Skip empty lines
                i += 1
                continue

            folio_match = self.folio_pat.search(line)
            if folio_match:
                current_folio = folio_match.group(1).replace(" ", "")
                
                header_lines_buffer = []
                # Look ahead for ~5 lines or until a transaction/new folio for header info
                for j in range(1, 6): 
                    if i + j < len(lines):
                        next_line_for_header = lines[i+j].strip()
                        if self.trans_details_pat.search(next_line_for_header) or self.folio_pat.search(next_line_for_header):
                            break
                        if next_line_for_header:
                            header_lines_buffer.append(next_line_for_header)
                    else:
                        break 
                
                full_header_block = "\n".join(header_lines_buffer)
                fn, isin, adv, reg = self._extract_funds_details(full_header_block)
                current_fund_details = {
                    "fund_name": fn, "isin": isin, "advisor": adv, "registrar": reg
                }
                i += 1 # Move past current folio line
                continue

            trans_match = self.trans_details_pat.search(line)
            if trans_match:
                date, description, amount, units, price, unit_bal = trans_match.groups()
                
                line_itms.append([
                    current_fund_details.get("fund_name"),
                    current_folio if current_folio else None,
                    date.strip(), units.strip(), price.strip(), unit_bal.strip(),
                    amount.strip(), description.strip(),
                    current_fund_details.get("isin"),
                    current_fund_details.get("advisor"),
                    current_fund_details.get("registrar")
                ])
            i += 1

        if not line_itms:
            self.error_occurred.emit("No transaction data found or parsed. The PDF might not contain recognizable transactions, or its format is not currently supported.")
            return

        df = DataFrame(
            line_itms,
            columns=[
                "Fund_name", "Folio", "Date", "Units", "Price", "Unit_balance",
                "Amount", "Description", "ISIN", "Advisor", "Registrar",
            ]
        )

        for col in ["Amount", "Units", "Price", "Unit_balance"]:
            # Clean numeric columns: remove commas, handle parentheses for negatives (e.g. (123.45) -> -123.45)
            s = df[col].astype(str).str.replace(',', '', regex=False)
            s = s.str.replace(r'^\((.*)\)$', r'-\1', regex=True)
            df[col] = pd.to_numeric(s, errors='coerce') # Coerce errors to NaN

        file_name = f'CAMS_data_{datetime.now().strftime("%d_%m_%Y_%H_%M_%S")}.csv'
        try:
            downloads_path = os.path.join(os.path.expanduser("~"), "Downloads")
            os.makedirs(downloads_path, exist_ok=True)
            save_file = os.path.join(downloads_path, file_name)

            df.to_csv(save_file, index=False)
            self.processing_done.emit(f"Process completed. File saved to:\n{save_file}")
        except Exception as e:
            raise ValueError(f"Failed to save the CSV file: {e}")


class WelcomeScreen(QDialog):
    def __init__(self):
        super().__init__()
        ui_path = os.path.join(basedir, "welcome.ui")
        if not os.path.exists(ui_path):
            QMessageBox.critical(self, "Error", f"UI file not found: {ui_path}")
            # Consider sys.exit(1) or raising an exception if UI is critical
            # For now, loadUi will likely fail and raise its own error.
        loadUi(ui_path, self)
        
        self.btn_browse.clicked.connect(self.file_dialog)
        self.chk_password.toggled.connect(self.enable_pw_input)
        self.btn_submit.clicked.connect(self.start_processing_thread)
        
        self.thread = None
        self.worker = None

        self.enable_pw_input() # Set initial state of password field

    def file_dialog(self):
        self.lbl_path.clear() # Clear path from previous selection
        # self.lbl_message.clear() # Clear messages when new file dialog opens
        filename, _ = QFileDialog.getOpenFileName(
            parent=self,
            caption="Select your CAMS PDF file",
            directory=os.getcwd(), # Or a remembered path
            filter="PDF files (*.pdf)",
        )
        if filename:
            self.lbl_path.setText(filename)
            self.lbl_message.clear() # Clear previous status/error messages

    def enable_pw_input(self):
        is_checked = self.chk_password.isChecked()
        self.le_pwd.setEnabled(is_checked)
        self.le_pwd.setPlaceholderText("Document Password" if is_checked else "")
        if not is_checked:
            self.le_pwd.clear()
        # No need to clear lbl_message here, it's for status/errors.

    def display_message(self, title, text, icon_type=QMessageBox.Information):
        """Utility to show a QMessageBox."""
        msg_box = QMessageBox(self)
        msg_box.setIcon(icon_type)
        msg_box.setWindowTitle(title)
        msg_box.setText(text)
        msg_box.exec_()

    def start_processing_thread(self):
        file_path = self.lbl_path.text()
        if not file_path:
            self.display_message("Input Error", "Please select your CAMS PDF file.", QMessageBox.Warning)
            return

        self.lbl_message.setText("Processing, please wait...")
        self.set_controls_enabled(False)

        doc_pwd = self.le_pwd.text() if self.chk_password.isChecked() else ""

        self.thread = QThread(self) # Parent thread to dialog for auto-cleanup (optional)
        self.worker = Worker(file_path, doc_pwd)
        self.worker.moveToThread(self.thread)

        self.thread.started.connect(self.worker.run)
        self.worker.processing_done.connect(self.on_processing_done)
        self.worker.error_occurred.connect(self.on_processing_error)
        
        # Cleanup when thread finishes its event loop (after quit() is called)
        self.thread.finished.connect(self.worker.deleteLater)
        self.thread.finished.connect(self.thread.deleteLater)
        self.thread.finished.connect(self.reset_thread_references)


        self.thread.start()

    def set_controls_enabled(self, enabled_status):
        """Enable or disable UI controls during processing."""
        self.btn_submit.setEnabled(enabled_status)
        self.btn_browse.setEnabled(enabled_status)
        self.chk_password.setEnabled(enabled_status)
        # Password field's state depends on checkbox, so call enable_pw_input if enabling
        if enabled_status:
            self.enable_pw_input()
        else:
            self.le_pwd.setEnabled(False)

    def on_processing_done(self, message):
        self.lbl_message.setText(message)
        # self.display_message("Success", message, QMessageBox.Information) # Alternative popup
        self.finalize_processing()

    def on_processing_error(self, error_message):
        self.lbl_message.clear() # Clear "Processing..."
        self.display_message("Error", error_message, QMessageBox.Critical)
        self.finalize_processing()

    def finalize_processing(self):
        """Common actions after processing finishes (success or error)."""
        self.set_controls_enabled(True)
        if self.thread and self.thread.isRunning(): # Should not be running if signals received, but check
            self.thread.quit() # Ask event loop to stop
            # self.thread.wait() # Usually not needed if using deleteLater correctly

    def reset_thread_references(self):
        """Called when thread.finished to clear worker/thread attributes."""
        self.worker = None
        self.thread = None
        # print("Thread and worker references reset.")


    def closeEvent(self, event):
        if self.thread and self.thread.isRunning():
            reply = QMessageBox.question(self, 'Confirm Close',
                                       "Processing is ongoing. Are you sure you want to close?",
                                       QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if reply == QMessageBox.Yes:
                self.thread.quit()
                if not self.thread.wait(3000): # Wait up to 3 seconds
                    print("Warning: Worker thread did not stop gracefully on close. Forcing termination.")
                    self.thread.terminate() # Use with caution: can lead to resource leaks or corrupted state
                    self.thread.wait() # Wait for termination to complete
                event.accept()
            else:
                event.ignore()
        else:
            event.accept()

if __name__ == "__main__":
    app = QApplication(sys.argv)
    
    widget = WelcomeScreen()
    widget.setWindowTitle("CAMS PDF Extractor")
    
    icon_path = os.path.join(basedir, "icons", "app_icon.svg")
    if os.path.exists(icon_path):
        widget.setWindowIcon(QtGui.QIcon(icon_path))
    else:
        print(f"Warning: Application icon not found at {icon_path}")
    
    widget.show()

    sys.exit(app.exec_())