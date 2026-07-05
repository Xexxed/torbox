import os
import sys
import unittest
import tempfile
import shutil
import http.server
import socketserver
import threading
from playwright.sync_api import sync_playwright
from torbox_downloader.creator import create_folder_structure
from torbox_downloader.detector import TorboxDetector
from torbox_downloader.downloader import download_file

class TestIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Set up a temporary directory for workspace testing
        cls.test_dir = tempfile.mkdtemp()
        
        # Write a mock file to download
        cls.mock_download_file = os.path.join(cls.test_dir, "source_file.bin")
        cls.mock_content = b"This is mock binary content for testing downloader."
        with open(cls.mock_download_file, "wb") as f:
            f.write(cls.mock_content)
            
        # Start a local HTTP server to serve files and mock downloads
        # We need this because Playwright and requests can fetch URLs from it
        class TestHTTPHandler(http.server.SimpleHTTPRequestHandler):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, directory=cls.test_dir, **kwargs)
            def log_message(self, format, *args):
                pass # suppress logging output to terminal

        cls.port = 8765
        cls.httpd = socketserver.TCPServer(("127.0.0.1", cls.port), TestHTTPHandler)
        cls.server_thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.server_thread.start()
        
        # Create a mock HTML page simulating the Torbox layout
        # We place it in the temp directory so the HTTP server can serve it
        cls.mock_html_content = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <title>Torbox Detail View</title>
            <style>
                .hidden {{ display: none; }}
                .options-panel {{ padding: 10px; background: #333; }}
            </style>
            <script>
                function toggleOptions(id) {{
                    var el = document.getElementById(id);
                    if (el.classList.contains('hidden')) {{
                        el.classList.remove('hidden');
                    }} else {{
                        el.classList.add('hidden');
                    }}
                }}
            </script>
        </head>
        <body>
            <div class="torrent-container">
                <!-- Main Header / Folder Title -->
                <h2 class="font-bold text-white header-title">Integration Pack [Test]</h2>
                
                <div class="files-list">
                    <!-- File Row 1 -->
                    <div class="file-row" id="row-file1">
                        <span>file1.txt</span>
                        <span class="file-size">45B</span>
                        <button class="download-options-btn" onclick="toggleOptions('opt-file1')">Options</button>
                        <div id="opt-file1" class="options-panel hidden">
                            <a href="http://127.0.0.1:{cls.port}/source_file.bin" download="file1.txt" class="direct-dl">Direct Download</a>
                        </div>
                    </div>

                    <!-- Subfolder Box: Subdir -->
                    <div class="border subfolder-box">
                        <div class="subfolder-title">Subdir</div>
                        <div style="padding-left: 20px;">
                            <!-- File inside Subdir -->
                            <div class="file-row" id="row-file2">
                                <span>file2.txt</span>
                                <span class="file-size">45B</span>
                                <button class="download-options-btn" onclick="toggleOptions('opt-file2')">Options</button>
                                <div id="opt-file2" class="options-panel hidden">
                                    <a href="http://127.0.0.1:{cls.port}/source_file.bin" download="file2.txt" class="direct-dl">Direct Download</a>
                                </div>
                            </div>
                        </div>
                    </div>
                </div>
            </div>
        </body>
        </html>
        """
        cls.mock_html_file = os.path.join(cls.test_dir, "mock_torbox.html")
        with open(cls.mock_html_file, "w", encoding="utf-8") as f:
            f.write(cls.mock_html_content)

    @classmethod
    def tearDownClass(cls):
        # Shut down HTTP server
        cls.httpd.shutdown()
        cls.httpd.server_close()
        # Clean up temporary directory
        shutil.rmtree(cls.test_dir)

    def test_end_to_end_automation(self):
        url = f"http://127.0.0.1:{self.port}/{os.path.basename(self.mock_html_file)}"
        dest_dir = os.path.join(self.test_dir, "output_downloads")
        
        # Run Playwright execution flow inline to test
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context()
            page = context.new_page()
            
            page.goto(url, wait_until="networkidle")
            
            # 1. Parse structure
            html_content = page.content()
            detector = TorboxDetector(html_content)
            files = detector.parse_structure()
            
            self.assertEqual(len(files), 2)
            self.assertEqual(files[0]['name'], "file1.txt")
            self.assertEqual(files[0]['path'], ["Integration Pack [Test]"])
            self.assertEqual(files[1]['name'], "file2.txt")
            self.assertEqual(files[1]['path'], ["Integration Pack [Test]", "Subdir"])
            
            # 2. Recreate folders
            files_with_local_paths = create_folder_structure(dest_dir, files)
            
            # 3. Simulate sequential clicks and downloading
            for file_info in files_with_local_paths:
                name = file_info['name']
                local_path = file_info['local_path']
                
                # Locate the button using text matching
                row_locator = page.locator("div.file-row", has_text=name).first
                dl_btn = row_locator.locator("button.download-options-btn")
                
                # Open download options
                dl_btn.click()
                page.wait_for_timeout(200) # small pause for rendering
                
                # Locate Direct Download option inside that row
                direct_dl_btn = row_locator.locator("a.direct-dl")
                
                # Intercept the download URL
                with page.expect_download() as download_info:
                    direct_dl_btn.click()
                    
                download = download_info.value
                download_url = download.url
                download.cancel()
                
                # Verify we intercepted the correct URL
                self.assertIn("source_file.bin", download_url)
                
                # Call downloader to save file
                success = download_file(download_url, local_path, expected_size_str="45B")
                self.assertTrue(success)
                self.assertTrue(os.path.exists(local_path))
                
                # Verify content matches
                with open(local_path, "rb") as f:
                    content = f.read()
                self.assertEqual(content, self.mock_content)
                
            browser.close()

if __name__ == '__main__':
    unittest.main()
