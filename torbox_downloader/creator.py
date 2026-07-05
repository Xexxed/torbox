import os
import logging

logger = logging.getLogger(__name__)

def create_folder_structure(base_dir, files_list):
    """
    Recreates the directory structure locally.
    """
    os.makedirs(base_dir, exist_ok=True)
    
    updated_files = []
    for file_info in files_list:
        name = file_info['name']
        path_segments = file_info.get('path', [])
        
        target_dir = os.path.join(base_dir, *path_segments)
        try:
            os.makedirs(target_dir, exist_ok=True)
        except Exception as e:
            logger.error(f"Failed to create directory {target_dir}: {e}")
            raise
            
        local_path = os.path.join(target_dir, name)
        
        updated_file_info = file_info.copy()
        updated_file_info['local_path'] = local_path
        updated_files.append(updated_file_info)
        
    return updated_files
