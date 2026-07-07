import unittest
from torbox_downloader.detector import TorboxDetector

class TestTorboxDetector(unittest.TestCase):
    def setUp(self):
        # Mock HTML structure representing the user's Torbox download details page.
        # This includes subfolders represented by cards/boxes with borders and titles,
        # visual indentation classes, file rows with names, sizes, and download buttons.
        self.mock_html = """
        <!DOCTYPE html>
        <html>
        <head>
            <title>Torbox Detail View</title>
        </head>
        <body>
            <div class="torrent-container">
                <!-- Main Header / Folder Title -->
                <h2 class="text-xl font-bold text-white header-title">Sleeping Dogs Pack [FitGirl Repack]</h2>
                
                <!-- Files List Wrapper -->
                <div class="files-list mt-4 flex flex-col gap-2">
                    
                    <!-- File Row 1 -->
                    <div class="flex items-center justify-between p-3 bg-gray-800 rounded-lg file-row">
                        <div class="flex items-center gap-3">
                            <span class="file-icon">📄</span>
                            <div>
                                <div class="font-semibold text-gray-200">fg-01.bin</div>
                                <div class="text-xs text-gray-400 file-size">2GB</div>
                            </div>
                        </div>
                        <button class="p-2 text-green-400 bg-gray-700 hover:bg-gray-600 rounded download-btn" aria-label="Download Options">
                            <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-4l-4 4m0 0l-4-4m4 4V4"></path></svg>
                        </button>
                    </div>

                    <!-- File Row 2 -->
                    <div class="flex items-center justify-between p-3 bg-gray-800 rounded-lg file-row">
                        <div class="flex items-center gap-3">
                            <span class="file-icon">📄</span>
                            <div>
                                <div class="font-semibold text-gray-200">fg-02.bin</div>
                                <div class="text-xs text-gray-400 file-size">2GB</div>
                            </div>
                        </div>
                        <button class="p-2 text-green-400 bg-gray-700 hover:bg-gray-600 rounded download-btn" aria-label="Download Options">
                            <svg class="w-5 h-5" fill="none"></svg>
                        </button>
                    </div>

                    <!-- Subfolder Container Box: MD5 -->
                    <div class="border border-orange-500 rounded-md p-4 mt-2 subfolder-box">
                        <!-- Subfolder Title -->
                        <div class="text-orange-500 font-bold mb-2 subfolder-title">MD5</div>
                        
                        <!-- Files inside Subfolder (Indented) -->
                        <div class="pl-4 flex flex-col gap-2">
                            <!-- File inside MD5 1 -->
                            <div class="flex items-center justify-between p-2 bg-gray-900 rounded file-row">
                                <div class="flex items-center gap-2">
                                    <span>📄</span>
                                    <div>
                                        <div class="text-sm font-medium">fitgirl-bins.md5</div>
                                        <div class="text-xs text-gray-500">45B</div>
                                    </div>
                                </div>
                                <button class="p-1 text-green-500 bg-gray-800 rounded download-btn" title="Download Options">
                                    <svg class="w-4 h-4"></svg>
                                </button>
                            </div>

                            <!-- File inside MD5 2 -->
                            <div class="flex items-center justify-between p-2 bg-gray-900 rounded file-row">
                                <div class="flex items-center gap-2">
                                    <span>📄</span>
                                    <div>
                                        <div class="text-sm font-medium">QuickSFV.EXE</div>
                                        <div class="text-xs text-gray-500">103KB</div>
                                    </div>
                                </div>
                                <button class="p-1 text-green-500 bg-gray-800 rounded download-btn" title="Download Options">
                                    <svg class="w-4 h-4"></svg>
                                </button>
                            </div>
                        </div>
                    </div>

                    <!-- File Row 3 (Back at main level) -->
                    <div class="flex items-center justify-between p-3 bg-gray-800 rounded-lg file-row">
                        <div class="flex items-center gap-3">
                            <span class="file-icon">📄</span>
                            <div>
                                <div class="font-semibold text-gray-200">setup.exe</div>
                                <div class="text-xs text-gray-400 file-size">6MB</div>
                            </div>
                        </div>
                        <button class="p-2 text-green-400 bg-gray-700 hover:bg-gray-600 rounded download-btn">
                            <svg class="w-5 h-5"></svg>
                        </button>
                    </div>

                </div>
            </div>
        </body>
        </html>
        """
        self.detector = TorboxDetector(self.mock_html)

    def test_main_title_detection(self):
        title = self.detector._detect_main_title()
        self.assertEqual(title, "Sleeping Dogs Pack [FitGirl Repack]")

    def test_file_parsing(self):
        files = self.detector.parse_structure()
        
        # Verify total file count
        self.assertEqual(len(files), 5)
        
        # Verify file 1 (main folder)
        self.assertEqual(files[0]['name'], "fg-01.bin")
        self.assertEqual(files[0]['size'], "2GB")
        self.assertEqual(files[0]['path'], ["Sleeping Dogs Pack [FitGirl Repack]"])
        
        # Verify file 2 (main folder)
        self.assertEqual(files[1]['name'], "fg-02.bin")
        self.assertEqual(files[1]['size'], "2GB")
        self.assertEqual(files[1]['path'], ["Sleeping Dogs Pack [FitGirl Repack]"])
        
        # Verify files in MD5 subfolder
        self.assertEqual(files[2]['name'], "fitgirl-bins.md5")
        self.assertEqual(files[2]['size'], "45B")
        self.assertEqual(files[2]['path'], ["Sleeping Dogs Pack [FitGirl Repack]", "MD5"])
        
        self.assertEqual(files[3]['name'], "QuickSFV.EXE")
        self.assertEqual(files[3]['size'], "103KB")
        self.assertEqual(files[3]['path'], ["Sleeping Dogs Pack [FitGirl Repack]", "MD5"])
        
        # Verify file 3 (back to main folder)
        self.assertEqual(files[4]['name'], "setup.exe")
        self.assertEqual(files[4]['size'], "6MB")
        self.assertEqual(files[4]['path'], ["Sleeping Dogs Pack [FitGirl Repack]"])

class TestDashboardExclusion(unittest.TestCase):
    def test_dashboard_cards_excluded_from_files(self):
        mock_dashboard_html = """
        <!DOCTYPE html>
        <html>
        <body>
            <div class="dashboard-container">
                <h1>Dashboard</h1>
                <!-- Torrent Card 1 -->
                <div class="flex items-center justify-between p-4 border rounded-lg bg-gray-800">
                    <div class="flex-1 min-w-0">
                        <p class="font-semibold text-gray-200 truncate">rutor.info_Hatred (2015) PC | RePack o r FitGirl</p>
                        <div class="flex gap-4 mt-1 text-xs text-gray-400">
                            <span>Total Size: <span class="font-semibold">177MB</span></span>
                        </div>
                    </div>
                    <div class="flex items-center gap-2">
                        <button title="Download files" class="p-2 text-blue-400 bg-gray-700 hover:bg-gray-600 rounded">
                            <svg class="w-5 h-5" fill="none"></svg>
                        </button>
                    </div>
                </div>
            </div>
        </body>
        </html>
        """
        detector = TorboxDetector(mock_dashboard_html)
        files = detector.parse_structure()
        self.assertEqual(len(files), 0, "Should detect zero files on the dashboard page because card items are excluded")

    def test_no_subfolder_creates_parent_directory(self):
        mock_html_no_subdirs = """
        <!DOCTYPE html>
        <html>
        <body>
            <div class="torrent-container border rounded p-4">
                <h3 class="font-bold text-white subfolder-title">Sample Show S01</h3>
                <div class="flex items-center justify-between p-2 border-b">
                    <span class="file-name">Sample.Show.S01E01.mkv</span>
                    <span class="file-size">450MB</span>
                    <button title="Download" class="dl-btn">Download</button>
                </div>
                <div class="flex items-center justify-between p-2 border-b">
                    <span class="file-name">read me.txt</span>
                    <span class="file-size">1KB</span>
                    <button title="Download" class="dl-btn">Download</button>
                </div>
            </div>
        </body>
        </html>
        """
        detector = TorboxDetector(mock_html_no_subdirs)
        detector.excluded_nodes = set()
        
        files = detector.parse_structure()
        self.assertEqual(len(files), 2)
        self.assertEqual(files[0]['name'], "Sample.Show.S01E01.mkv")
        self.assertEqual(files[0]['path'], ["Sample Show S01"])
        self.assertEqual(files[1]['name'], "read me.txt")
        self.assertEqual(files[1]['path'], ["Sample Show S01"])

class TestSelectiveDownloads(unittest.TestCase):
    def test_selected_files_path_filtering(self):
        # Mock file list as returned by TorboxDetector.parse_structure()
        files = [
            {"name": "file1.txt", "path": ["TorrentFolder"]},
            {"name": "file2.txt", "path": ["TorrentFolder", "Subdir"]},
            {"name": "file3.txt", "path": ["TorrentFolder", "Subdir", "Nested"]},
        ]
        
        # Test 1: select only file2.txt by its relative path
        selected_files_1 = ["TorrentFolder/Subdir/file2.txt"]
        filtered_1 = [f for f in files if "/".join(f.get('path', []) + [f['name']]) in selected_files_1]
        self.assertEqual(len(filtered_1), 1)
        self.assertEqual(filtered_1[0]['name'], "file2.txt")
        
        # Test 2: select multiple files
        selected_files_2 = ["TorrentFolder/file1.txt", "TorrentFolder/Subdir/Nested/file3.txt"]
        filtered_2 = [f for f in files if "/".join(f.get('path', []) + [f['name']]) in selected_files_2]
        self.assertEqual(len(filtered_2), 2)
        self.assertEqual(filtered_2[0]['name'], "file1.txt")
        self.assertEqual(filtered_2[1]['name'], "file3.txt")

if __name__ == '__main__':
    unittest.main()
