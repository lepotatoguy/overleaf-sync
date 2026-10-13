"""Overleaf Client"""
##################################################
# MIT License
##################################################
# File: olclient.py
# Description: Overleaf API Wrapper
# Author: Moritz Glöckl
# License: MIT
# Version: 1.2.0
##################################################

import requests as reqs
from bs4 import BeautifulSoup
import json
import mimetypes
from socketIO_client import SocketIO
import time

# Where to get the CSRF Token and where to send the login request to
LOGIN_URL = "https://www.overleaf.com/login"
PROJECT_URL = "https://www.overleaf.com/project"  # The dashboard URL
# The URL to download all the files in zip format
DOWNLOAD_URL = "https://www.overleaf.com/project/{}/download/zip"
UPLOAD_URL = "https://www.overleaf.com/project/{}/upload"  # The URL to upload files
FOLDER_URL = "https://www.overleaf.com/project/{}/folder"  # The URL to create folders
DELETE_URL = "https://www.overleaf.com/project/{}/doc/{}"  # The URL to delete files
COMPILE_URL = "https://www.overleaf.com/project/{}/compile?enable_pdf_caching=true"  # The URL to compile the project
BASE_URL = "https://www.overleaf.com"  # The Overleaf Base URL
PATH_SEP = "/"  # Use hardcoded path separator for both windows and posix system
JOIN_PROJECT_TIMEOUT = 20  # Seconds to wait for the project details from the Overleaf Socket.IO

class OverleafClient(object):
    """
    Overleaf API Wrapper
    Supports login, querying all projects, querying a specific project, downloading a project and
    uploading a file to a project.
    """

    @staticmethod
    def filter_projects(json_content, more_attrs=None):
        more_attrs = more_attrs or {}
        for p in json_content:
            if not p.get("archived") and not p.get("trashed"):
                if all(p.get(k) == v for k, v in more_attrs.items()):
                    yield p

    def __init__(self, cookie=None, csrf=None):
        self._cookie = cookie  # Store the cookie for authenticated requests
        self._csrf = csrf  # Store the CSRF token since it is needed for some requests

    def login(self, username, password):
        """
        WARNING - DEPRECATED - Not working as Overleaf introduced captchas
        Login to the Overleaf Service with a username and a password
        Params: username, password
        Returns: Dict of cookie and CSRF
        """

        get_login = reqs.get(LOGIN_URL)
        self._csrf = BeautifulSoup(get_login.content, 'html.parser').find(
            'input', {'name': '_csrf'}).get('value')
        login_json = {
            "_csrf": self._csrf,
            "email": username,
            "password": password
        }
        post_login = reqs.post(LOGIN_URL, json=login_json,
                               cookies=get_login.cookies)

        # On a successful authentication the Overleaf API returns a new authenticated cookie.
        # If the cookie is different than the cookie of the GET request the authentication was successful
        if post_login.status_code == 200 and get_login.cookies["overleaf_session2"] != post_login.cookies[
            "overleaf_session2"]:
            self._cookie = post_login.cookies

            # Enrich cookie with GCLB cookie from GET request above
            self._cookie['GCLB'] = get_login.cookies['GCLB']

            # CSRF changes after making the login request, new CSRF token will be on the projects page
            projects_page = reqs.get(PROJECT_URL, cookies=self._cookie)
            self._csrf = BeautifulSoup(projects_page.content, 'html.parser').find('meta', {'name': 'ol-csrfToken'}) \
                .get('content')

            return {"cookie": self._cookie, "csrf": self._csrf}

    def _fetch_projects(self):
        """
        Scrape the project list from the dashboard
        Newer Overleaf versions embed it as {"totalSize", "projects"} in the `ol-prefetchedProjectsBlob` meta tag;
        older versions used a plain list in `ol-projects`.
        Returns: List of raw project objects
        """
        projects_page = reqs.get(PROJECT_URL, cookies=self._cookie)
        projects_page.raise_for_status()
        soup = BeautifulSoup(projects_page.content, 'html.parser')

        blob_meta = soup.find('meta', {'name': 'ol-prefetchedProjectsBlob'})
        if blob_meta is not None:
            return json.loads(blob_meta.get('content'))['projects']

        legacy_meta = soup.find('meta', {'name': 'ol-projects'})
        if legacy_meta is not None:
            return json.loads(legacy_meta.get('content'))

        raise reqs.HTTPError("Project list not found on dashboard. Is the persisted cookie still valid?")

    def all_projects(self):
        """
        Get all of a user's active projects (= not archived and not trashed)
        Returns: List of project objects
        """
        return list(OverleafClient.filter_projects(self._fetch_projects()))

    def get_project(self, project_name):
        """
        Get a specific project by project_name
        Params: project_name, the name of the project
        Returns: project object
        """

        return next(OverleafClient.filter_projects(self._fetch_projects(), {"name": project_name}), None)

    def download_project(self, project_id):
        """
        Download project in zip format
        Params: project_id, the id of the project
        Returns: bytes string (zip file)
        """
        r = reqs.get(DOWNLOAD_URL.format(project_id),
                     stream=True, cookies=self._cookie)
        return r.content

    def create_folder(self, project_id, parent_folder_id, folder_name):
        """
        Create a new folder in a project

        Params:
        project_id: the id of the project
        parent_folder_id: the id of the parent folder, root is the project_id
        folder_name: how the folder will be named

        Returns: folder id or None
        """

        params = {
            "parent_folder_id": parent_folder_id,
            "name": folder_name
        }
        headers = {
            "X-Csrf-Token": self._csrf
        }
        r = reqs.post(FOLDER_URL.format(project_id),
                      cookies=self._cookie, headers=headers, json=params)

        if r.ok:
            return json.loads(r.content)
        elif r.status_code == 400:
            # Folder already exists
            return
        else:
            raise reqs.HTTPError()

    def get_project_infos(self, project_id):
        """
        Get detailed project infos about the project

        Params:
        project_id: the id of the project

        Returns: project details
        """
        project_infos = None

        # Callback for the joinProjectResponse event, which the server emits right after connecting
        def set_project_infos(response, *_):
            # Set project_infos variable in outer scope
            nonlocal project_infos
            project_infos = response.get('project')

        # Convert cookies (dict or CookieJar, GCLB is no longer set by Overleaf) to a header string
        cookie = "; ".join("{}={}".format(k, v) for k, v in dict(self._cookie).items())

        # Connect to Overleaf Socket.IO. The project is now joined via the `projectId` query parameter
        # instead of a `joinProject` emit. The websocket upgrade fails with 502, so use xhr-polling.
        socket_io = SocketIO(
            BASE_URL,
            params={'t': int(time.time()), 'projectId': project_id},
            headers={'Cookie': cookie},
            transports=['xhr-polling'],
            # socketIO-client defaults to verify=False, which would send the session cookie without TLS verification
            verify=True
        )
        socket_io.on('joinProjectResponse', set_project_infos)

        deadline = time.time() + JOIN_PROJECT_TIMEOUT
        while project_infos is None and time.time() < deadline:
            socket_io.wait(seconds=1)

        # Disconnect from the socket if still connected
        if socket_io.connected:
            socket_io.disconnect()

        if project_infos is None:
            raise reqs.HTTPError("No joinProjectResponse received from Overleaf within {}s".format(
                JOIN_PROJECT_TIMEOUT))

        return project_infos

    def upload_file(self, project_id, project_infos, file_name, file_size, file):
        """
        Upload a file to the project

        Params:
        project_id: the id of the project
        file_name: how the file will be named
        file_size: the size of the file in bytes
        file: the file itself

        Returns: True on success, False on fail
        """

        # Set the folder_id to the id of the root folder
        folder_id = project_infos['rootFolder'][0]['_id']

        # The file name contains path separators, check folders
        if PATH_SEP in file_name:
            local_folders = file_name.split(PATH_SEP)[:-1]  # Remove last item since this is the file name
            current_overleaf_folder = project_infos['rootFolder'][0]['folders']  # Set the current remote folder

            for local_folder in local_folders:
                exists_on_remote = False
                for remote_folder in current_overleaf_folder:
                    # Check if the folder exists on remote, continue with the new folder structure
                    if local_folder.lower() == remote_folder['name'].lower():
                        exists_on_remote = True
                        folder_id = remote_folder['_id']
                        current_overleaf_folder = remote_folder['folders']
                        break
                # Create the folder if it doesn't exist
                if not exists_on_remote:
                    new_folder = self.create_folder(project_id, folder_id, local_folder)
                    current_overleaf_folder.append(new_folder)
                    folder_id = new_folder['_id']
                    current_overleaf_folder = new_folder['folders']
        # The current upload endpoint reads the file name from the `name` form field (the old qq* query
        # parameters now give 422 invalid_filename) and expects the CSRF token as a header
        base_name = file_name.split(PATH_SEP)[-1]
        content_type = mimetypes.guess_type(base_name)[0] or "application/octet-stream"
        params = {
            "folder_id": folder_id,
        }
        headers = {
            "X-Csrf-Token": self._csrf
        }
        data = {
            "name": base_name,
            "type": content_type,
            "relativePath": "null",
        }
        files = {
            "qqfile": (base_name, file, content_type)
        }

        # Upload the file to the predefined folder
        r = reqs.post(UPLOAD_URL.format(project_id), cookies=self._cookie, params=params, headers=headers,
                      data=data, files=files)

        # Raise instead of returning False, the sync loop ignores return values and would report success
        if not (r.status_code == 200 and json.loads(r.content).get("success")):
            raise reqs.HTTPError("Upload of {} failed with status {}".format(file_name, r.status_code))
        return True

    def delete_file(self, project_id, project_infos, file_name):
        """
        Deletes a project's file

        Params:
        project_id: the id of the project
        file_name: how the file will be named

        Returns: True on success, False on fail
        """

        file = None

        # The file name contains path separators, check folders
        if PATH_SEP in file_name:
            local_folders = file_name.split(PATH_SEP)[:-1]  # Remove last item since this is the file name
            current_overleaf_folder = project_infos['rootFolder'][0]['folders']  # Set the current remote folder

            for local_folder in local_folders:
                for remote_folder in current_overleaf_folder:
                    if local_folder.lower() == remote_folder['name'].lower():
                        file = next((v for v in remote_folder['docs'] if v['name'] == file_name.split(PATH_SEP)[-1]),
                                    None)
                        current_overleaf_folder = remote_folder['folders']
                        break
        # File is in root folder
        else:
            file = next((v for v in project_infos['rootFolder'][0]['docs'] if v['name'] == file_name), None)

        # File not found!
        if file is None:
            return False

        headers = {
            "X-Csrf-Token": self._csrf
        }

        r = reqs.delete(DELETE_URL.format(project_id, file['_id']), cookies=self._cookie, headers=headers, json={})

        return r.status_code == 204

    def download_pdf(self, project_id):
        """
        Compiles and returns a project's PDF

        Params:
        project_id: the id of the project

        Returns: PDF file name and content on success
        """
        headers = {
            "X-Csrf-Token": self._csrf
        }

        body = {
            "check": "silent",
            "draft": False,
            "incrementalCompilesEnabled": True,
            "rootDoc_id": "",
            "stopOnFirstError": False
        }

        r = reqs.post(COMPILE_URL.format(project_id), cookies=self._cookie, headers=headers, json=body)

        if not r.ok:
            raise reqs.HTTPError()

        compile_result = json.loads(r.content)

        if compile_result["status"] != "success":
            raise reqs.HTTPError()

        pdf_file = next(v for v in compile_result['outputFiles'] if v['type'] == 'pdf')

        download_req = reqs.get(BASE_URL + pdf_file['url'], cookies=self._cookie, headers=headers)

        if download_req.ok:
            return pdf_file['path'], download_req.content

        return None
