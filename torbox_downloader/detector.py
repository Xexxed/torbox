import os
import re
from bs4 import BeautifulSoup

class TorboxDetector:
    def __init__(self, html_content=None):
        self.soup = BeautifulSoup(html_content, 'html.parser') if html_content else None

    def set_html(self, html_content):
        self.soup = BeautifulSoup(html_content, 'html.parser')

    def parse_torrents(self):
        if not self.soup:
            return []
            
        torrents = []
        card_containers = []
        
        for btn in self.soup.find_all('button', title='Download files'):
            parent = btn.parent
            while parent and parent.name not in ['body', 'html', '[document]']:
                if parent.name == 'div':
                    title_el = parent.find('p', class_=re.compile(r'text-base|text-lg|font-semibold'))
                    if title_el:
                        card_containers.append((parent, btn, title_el))
                        break
                parent = parent.parent
                    
        seen_names = set()
        for container, btn, title_el in card_containers:
            name = title_el.text.strip()
            if name in seen_names:
                continue
            seen_names.add(name)
            
            size = "Unknown"
            size_el = container.find(string=re.compile(r'Total Size|Total size|Size', re.I))
            if size_el:
                p_el = size_el.parent
                span_el = p_el.find('span', class_=re.compile(r'font-semibold|font-bold')) if p_el else None
                if span_el:
                    size = span_el.text.strip()
                elif p_el:
                    # Clean label text
                    size = p_el.text.replace('Total Size', '').replace('Total size', '').replace('Size', '').strip()
            
            if size == "Unknown":
                for text_node in container.find_all(string=True):
                    if 'Total Size' in text_node or 'Total size' in text_node or 'Size' in text_node:
                        match = re.search(r'([\d.]+\s*(?:GB|MB|KB|B))', text_node, re.I)
                        if match:
                            size = match.group(1)
                            break
                            
            torrents.append({
                'name': name,
                'size': size
            })
            
        return torrents

    def parse_structure(self):
        
        if not self.soup:
            return []

        for tag in self.soup.find_all(['style', 'script']):
            tag.decompose()

        # Use id()-based sets for O(1) identity lookups instead of O(N) deep equality
        self._excluded_node_ids = set()
        for btn in self.soup.find_all('button', title='Download files'):
            parent = btn.parent
            while parent and parent.name not in ['body', 'html', '[document]']:
                if parent.name == 'div':
                    title_el = parent.find(['p', 'span', 'div'], class_=re.compile(r'text-base|text-lg|font-semibold|truncate'))
                    if title_el or parent.find('button', title='Download files'):
                        self._excluded_node_ids.add(id(parent))
                        for desc in parent.descendants:
                            self._excluded_node_ids.add(id(desc))
                parent = parent.parent

        main_title = self._detect_main_title()
        
        file_elements = []
        for tag in ['div', 'li', 'tr']:
            for el in self.soup.find_all(tag):
                if self._is_file_row(el):
                    file_elements.append(el)
                    
        # Use id()-based set for O(1) leaf filtering
        file_ids = set(id(el) for el in file_elements)
        parent_ids = set()
        for el in file_elements:
            p = el.parent
            while p and p.name not in ['body', 'html', '[document]']:
                if id(p) in file_ids:
                    parent_ids.add(id(p))
                p = p.parent
                
        leaf_file_elements = [el for el in file_elements if id(el) not in parent_ids]

        # Pre-compute file_row_ids for subfolder container checks
        self._file_row_ids = set(id(el) for el in file_elements)
        
        # Cache subfolder container results by element id
        self._subfolder_cache = {}

        files = []
        for el in leaf_file_elements:
            path = []
            parent = el.parent
            while parent and parent.name != '[document]':
                is_folder, folder_name = self._is_subfolder_container(parent)
                if is_folder and folder_name:
                    path.insert(0, folder_name)
                parent = parent.parent
                
            if main_title:
                path.insert(0, main_title)
                
            dedup_path = []
            for item in path:
                if not dedup_path or dedup_path[-1] != item:
                    dedup_path.append(item)
            path = dedup_path
                
            file_info = self._parse_file_row(el, path)
            if file_info:
                files.append(file_info)
                
        return files

    def _detect_main_title(self):
        """
        Detects the main torrent folder title.
        """
        for header_tag in ['h1', 'h2', 'h3']:
            header = self.soup.find(header_tag)
            if header and header.text.strip():
                # Avoid matching folder containers or file items as the main title
                text = header.text.strip()
                if not any(ext in text.lower() for ext in ['.bin', '.exe', '.bat', '.rar', '.zip', '.mp4']):
                    return text
        
        title_candidates = self.soup.find_all(class_=re.compile(r'title|header|font-bold|text-xl|text-2xl', re.I))
        for cand in title_candidates:
            text = cand.text.strip()
            if text and len(text) > 3 and not any(ext in text.lower() for ext in ['.bin', '.exe', '.bat', '.rar', '.zip', '.mp4']):
                return text
                
        return "Torbox_Download"

    def _is_file_row(self, element):
        """
        Determines if a DOM element represents a single file row without recursing.
        """
        # Exclude dashboard torrent card containers and any of their descendants
        if id(element) in self._excluded_node_ids:
            return False

        text = element.get_text(separator=' ').strip()
        if not text:
            return False

        # Must have a size indicator (e.g., 2GB, 316MB, 45B)
        size_match = re.search(r'\b\d+(?:\.\d+)?\s*(?:GB|MB|KB|B)\b', text, re.I)
        if not size_match:
            return False

        # Single-pass child tag scan for maximum accuracy and speed
        download_indicators = ['download', 'dl-', 'btn', 'icon', 'svg']
        for item in element.find_all(['button', 'a', 'div', 'svg']):
            classes = item.get('class', [])
            if any(any(ind in str(cls).lower() for ind in download_indicators) for cls in classes):
                return True
            for attr in ['id', 'aria-label', 'title']:
                val = item.get(attr, '')
                if any(ind in str(val).lower() for ind in download_indicators):
                    return True

        return False

    def _parse_file_row(self, element, current_path):
        """
        Extracts filename, size, and creates a unique identifier/selector for the file row.
        """
        text_nodes = [t.strip() for t in element.find_all(string=True) if t.strip()]
        if not text_nodes:
            return None

        size_pattern = r'^\d+(?:\.\d+)?\s*(?:GB|MB|KB|B)$'
        filename = ""
        size = ""

        for node in text_nodes:
            if re.match(size_pattern, node, re.I):
                size = node
            elif not filename and len(node) > 2 and not node.lower().startswith('download') and not any(ind in node.lower() for ind in ['option', 'direct', 'copy']):
                filename = node

        if not filename:
            filename = text_nodes[0]

        if not size:
            full_text = element.get_text(separator=' ').strip()
            size_match = re.search(r'\b\d+(?:\.\d+)?\s*(?:GB|MB|KB|B)\b', full_text, re.I)
            if size_match:
                size = size_match.group(0)

        selector = self._get_css_selector(element)

        return {
            'name': filename,
            'path': list(current_path),
            'size': size,
            'selector': selector,
            'text': filename
        }

    def _is_subfolder_container(self, element):
        """
        Determines if a DOM element represents a subfolder box/card.
        Uses id()-based caching and pre-computed file_row_ids to avoid
        expensive recursive _is_file_row() calls.
        """
        eid = id(element)
        if eid in self._subfolder_cache:
            return self._subfolder_cache[eid]
        
        result = self._is_subfolder_container_impl(element)
        self._subfolder_cache[eid] = result
        return result
    
    def _is_subfolder_container_impl(self, element):
        if element.name != 'div':
            return False, None

        classes = element.get('class', [])
        class_str = ' '.join(classes).lower()

        has_border = any(x in class_str for x in ['border', 'rounded', 'card', 'box', 'subfolder', 'folder'])
        if not has_border:
            style = element.get('style', '').lower()
            if 'border' in style:
                has_border = True

        if not has_border:
            return False, None

        # Try specific subfolder/card title classes first
        title_classes = ['subfolder-title', 'subfolder-header', 'folder-title', 'torrent-title', 'header-title']
        for cls in title_classes:
            header = element.find(class_=re.compile(cls, re.I))
            if header:
                header_text = header.text.strip()
                if header_text and len(header_text) < 100:
                    return True, header_text

        _FILE_EXTS = frozenset([
            '.bin', '.exe', '.bat', '.rar', '.zip', '.mp4', '.url',
            '.md5', '.ini', '.txt', '.sfv', '.nfo', '.pdf', '.mkv',
            '.avi', '.iso', '.cfg', '.dat', '.pkg', '.png', '.jpg',
            '.jpeg', '.gif', '.mp3', '.wav', '.flac'
        ])
        _SIZE_RE = re.compile(r'^\d+(?:\.\d+)?\s*(?:GB|MB|KB|B)$', re.I)

        # Fallback to direct children tags
        for tag in ['h3', 'h4', 'h5', 'div', 'span']:
            for header in element.find_all(tag, recursive=False):
                header_text = header.text.strip()
                if (header_text and len(header_text) < 100 and 
                        not any(ext in header_text.lower() for ext in _FILE_EXTS) and
                        not _SIZE_RE.match(header_text)):
                    return True, header_text

        # Safe recursive fallback using pre-computed file_row_ids (O(1) checks)
        # instead of calling _is_file_row() which does expensive DOM traversals
        for tag in ['h3', 'h4', 'h5', 'div', 'span']:
            for header in element.find_all(tag):
                # Check if any parent (up to this element) is a known file row
                skip = False
                p = header.parent
                while p and p is not element:
                    if id(p) in self._file_row_ids:
                        skip = True
                        break
                    p = p.parent
                if skip:
                    continue
                header_text = header.text.strip()
                if (header_text and len(header_text) < 100 and 
                        not any(ext in header_text.lower() for ext in _FILE_EXTS) and
                        not _SIZE_RE.match(header_text)):
                    return True, header_text

        return False, None

    def _get_css_selector(self, element):
        path = []
        while element and element.name != '[document]':
            name = element.name
            classes = element.get('class', [])
            if classes:
                valid_classes = [c for c in classes if not any(char in c for char in ['[', ']', ':', '#', '/', '@', '%', '.', '$'])]
                if valid_classes:
                    name += '.' + '.'.join(valid_classes)
            siblings = element.find_previous_siblings(element.name)
            if siblings:
                name += f":nth-of-type({len(siblings) + 1})"
            path.append(name)
            element = element.parent
        return ' > '.join(reversed(path))
