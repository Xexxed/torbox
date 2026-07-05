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
            for _ in range(6):
                if parent and parent.name == 'div':
                    title_el = parent.find('p', class_=re.compile(r'text-base|text-lg|font-semibold'))
                    if title_el:
                        card_containers.append((parent, btn, title_el))
                        break
                if parent:
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

        self.excluded_nodes = set()
        for btn in self.soup.find_all('button', title='Download files'):
            parent = btn.parent
            for _ in range(6):
                if parent and parent.name == 'div':
                    title_el = parent.find(['p', 'span', 'div'], class_=re.compile(r'text-base|text-lg|font-semibold|truncate'))
                    if title_el or parent.find('button', title='Download files'):
                        self.excluded_nodes.add(parent)
                        for desc in parent.descendants:
                            self.excluded_nodes.add(desc)
                if parent:
                    parent = parent.parent

        main_title = self._detect_main_title()
        
        file_elements = []
        for tag in ['div', 'li', 'tr']:
            for el in self.soup.find_all(tag):
                if self._is_file_row(el):
                    file_elements.append(el)
                    
        leaf_file_elements = []
        for el in file_elements:
            is_leaf = True
            for other in file_elements:
                if other != el and other in el.descendants:
                    is_leaf = False
                    break
            if is_leaf:
                leaf_file_elements.append(el)

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
        if hasattr(self, 'excluded_nodes') and element in self.excluded_nodes:
            return False

        text = element.get_text(separator=' ').strip()
        if not text:
            return False

        # Must have a size indicator (e.g., 2GB, 316MB, 45B)
        size_match = re.search(r'\b\d+(?:\.\d+)?\s*(?:GB|MB|KB|B)\b', text, re.I)
        if not size_match:
            return False

        # Must contain a download button/icon
        has_download = False
        download_indicators = ['download', 'dl-', 'btn', 'icon', 'svg']
        for tag in ['button', 'a', 'div', 'svg']:
            for item in element.find_all(tag):
                classes = item.get('class', [])
                if any(any(ind in str(cls).lower() for ind in download_indicators) for cls in classes):
                    has_download = True
                    break
                for attr in ['id', 'aria-label', 'title']:
                    val = item.get(attr, '')
                    if any(ind in str(val).lower() for ind in download_indicators):
                        has_download = True
                        break
            if has_download:
                break

        return has_download

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
        Determines if a DOM element represents a subfolder box/card without recursing.
        """
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

        # Fallback to direct children tags
        for tag in ['h3', 'h4', 'h5', 'div', 'span']:
            for header in element.find_all(tag, recursive=False):
                header_text = header.text.strip()
                if (header_text and len(header_text) < 100 and 
                        not any(ext in header_text.lower() for ext in [
                            '.bin', '.exe', '.bat', '.rar', '.zip', '.mp4', '.url',
                            '.md5', '.ini', '.txt', '.sfv', '.nfo', '.pdf', '.mkv',
                            '.avi', '.iso', '.cfg', '.dat', '.pkg', '.png', '.jpg',
                            '.jpeg', '.gif', '.mp3', '.wav', '.flac'
                        ]) and
                        not re.match(r'^\d+(?:\.\d+)?\s*(?:GB|MB|KB|B)$', header_text, re.I)):
                    return True, header_text

        # Safe recursive fallback (excluding elements that are inside file rows)
        for tag in ['h3', 'h4', 'h5', 'div', 'span']:
            for header in element.find_all(tag):
                if any(self._is_file_row(p) for p in header.parents if p != element):
                    continue
                header_text = header.text.strip()
                if (header_text and len(header_text) < 100 and 
                        not any(ext in header_text.lower() for ext in [
                            '.bin', '.exe', '.bat', '.rar', '.zip', '.mp4', '.url',
                            '.md5', '.ini', '.txt', '.sfv', '.nfo', '.pdf', '.mkv',
                            '.avi', '.iso', '.cfg', '.dat', '.pkg', '.png', '.jpg',
                            '.jpeg', '.gif', '.mp3', '.wav', '.flac'
                        ]) and
                        not re.match(r'^\d+(?:\.\d+)?\s*(?:GB|MB|KB|B)$', header_text, re.I)):
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
