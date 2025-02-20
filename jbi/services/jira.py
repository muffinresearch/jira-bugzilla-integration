"""Contains a Jira REST client and functions comprised of common operations
with that REST client
"""
# This import is needed (as of Pyhon 3.11) to enable type checking with modules
# imported under `TYPE_CHECKING`
# https://docs.python.org/3/whatsnew/3.7.html#pep-563-postponed-evaluation-of-annotations
# https://docs.python.org/3/whatsnew/3.11.html#pep-563-may-not-be-the-future
from __future__ import annotations

import concurrent.futures
import json
import logging
from functools import lru_cache
from typing import TYPE_CHECKING, Any, Iterable, Optional

import requests
from atlassian import Jira
from atlassian import errors as atlassian_errors
from requests import exceptions as requests_exceptions

from jbi import Operation, environment
from jbi.models import ActionContext, BugzillaBug

from .common import ServiceHealth, instrument

# https://docs.python.org/3.11/library/typing.html#typing.TYPE_CHECKING
if TYPE_CHECKING:
    from jbi.models import Actions

settings = environment.get_settings()

logger = logging.getLogger(__name__)


JIRA_DESCRIPTION_CHAR_LIMIT = 32767

JIRA_REQUIRED_PERMISSIONS = {
    "ADD_COMMENTS",
    "CREATE_ISSUES",
    "DELETE_ISSUES",
    "EDIT_ISSUES",
}


def fatal_code(exc):
    """Do not retry 4XX errors, mark them as fatal."""
    try:
        return 400 <= exc.response.status_code < 500
    except AttributeError:
        # `ApiError` or `ConnectionError` won't have response attribute.
        return False


instrumented_method = instrument(
    prefix="jira",
    exceptions=(
        atlassian_errors.ApiError,
        requests_exceptions.RequestException,
    ),
    giveup=fatal_code,
)


class JiraCreateError(Exception):
    """Error raised on Jira issue creation."""


class JiraClient(Jira):
    """Adapted Atlassian Jira client that logs errors and wraps methods
    in our instrumentation decorator.
    """

    def raise_for_status(self, *args, **kwargs):
        """Catch and log HTTP errors responses of the Jira self.client.

        Without this the actual requests and responses are not exposed when an error
        occurs, which makes troubleshooting tedious.
        """
        try:
            return super().raise_for_status(*args, **kwargs)
        except requests.HTTPError as exc:
            request = exc.request
            response = exc.response
            logger.error(
                "HTTP: %s %s -> %s %s",
                request.method,
                request.path_url,
                response.status_code,
                response.reason,
                extra={"body": response.text},
            )
            raise

    get_server_info = instrumented_method(Jira.get_server_info)
    get_permissions = instrumented_method(Jira.get_permissions)
    get_project_components = instrumented_method(Jira.get_project_components)
    projects = instrumented_method(Jira.projects)
    update_issue = instrumented_method(Jira.update_issue)
    update_issue_field = instrumented_method(Jira.update_issue_field)
    set_issue_status = instrumented_method(Jira.set_issue_status)
    issue_add_comment = instrumented_method(Jira.issue_add_comment)
    create_issue = instrumented_method(Jira.create_issue)
    get_project = instrumented_method(Jira.get_project)


class JiraService:
    """Used by action workflows to perform action-specific Jira tasks"""

    def __init__(self, client) -> None:
        self.client = client

    def fetch_visible_projects(self) -> list[dict]:
        """Return list of projects that are visible with the configured Jira credentials"""

        projects: list[dict] = self.client.projects(included_archived=None)
        return projects

    def check_health(self, actions: Actions) -> ServiceHealth:
        """Check health for Jira Service"""

        server_info = self.client.get_server_info(True)
        is_up = server_info is not None
        health: ServiceHealth = {
            "up": is_up,
            "all_projects_are_visible": is_up and self._all_projects_visible(actions),
            "all_projects_have_permissions": self._all_projects_permissions(actions),
            "all_projects_components_exist": is_up
            and self._all_projects_components_exist(actions),
            "all_projects_issue_types_exist": is_up
            and self._all_project_issue_types_exist(actions),
        }
        return health

    def _all_projects_visible(self, actions: Actions) -> bool:
        visible_projects = {project["key"] for project in self.fetch_visible_projects()}
        missing_projects = actions.configured_jira_projects_keys - visible_projects
        if missing_projects:
            logger.error(
                "Jira projects %s are not visible with configured credentials",
                missing_projects,
            )
        return not missing_projects

    def _all_projects_permissions(self, actions: Actions):
        """Fetches and validates that required permissions exist for the configured projects"""
        all_projects_perms = self._fetch_project_permissions(actions)
        return self._validate_permissions(all_projects_perms)

    def _fetch_project_permissions(self, actions: Actions):
        """Fetches permissions for the configured projects"""

        all_projects_perms = {}
        # Query permissions for all configured projects in parallel threads.
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
            futures_to_projects = {
                executor.submit(
                    self.client.get_permissions,
                    project_key=project_key,
                    permissions=",".join(JIRA_REQUIRED_PERMISSIONS),
                ): project_key
                for project_key in actions.configured_jira_projects_keys
            }
            # Obtain futures' results unordered.
            for future in concurrent.futures.as_completed(futures_to_projects):
                project_key = futures_to_projects[future]
                response = future.result()
                all_projects_perms[project_key] = response["permissions"]
        return all_projects_perms

    def _validate_permissions(self, all_projects_perms):
        """Validates permissions for the configured projects"""
        misconfigured = []
        for project_key, obtained_perms in all_projects_perms.items():
            missing_required_perms = JIRA_REQUIRED_PERMISSIONS - set(
                obtained_perms.keys()
            )
            not_given = set(
                entry["key"]
                for entry in obtained_perms.values()
                if not entry["havePermission"]
            )
            if missing_permissions := missing_required_perms.union(not_given):
                misconfigured.append((project_key, missing_permissions))
        for project_key, missing in misconfigured:
            logger.error(
                "Configured credentials don't have permissions %s on Jira project %s",
                ",".join(missing),
                project_key,
                extra={
                    "jira": {
                        "project": project_key,
                    }
                },
            )
        return not misconfigured

    def _all_projects_components_exist(self, actions: Actions):
        components_by_project = {
            action.parameters.jira_project_key: action.parameters.jira_components.set_custom_components
            for action in actions
        }
        success = True
        for project, specified_components in components_by_project.items():
            all_project_components = self.client.get_project_components(project)
            all_components_names = set(comp["name"] for comp in all_project_components)
            unknown = set(specified_components) - all_components_names
            if unknown:
                logger.error(
                    "Jira project %s does not have components %s",
                    project,
                    unknown,
                )
                success = False

        return success

    def _all_project_issue_types_exist(self, actions: Actions):
        issue_types_by_project = {
            action.parameters.jira_project_key: set(
                action.parameters.issue_type_map.values()
            )
            for action in actions
        }
        success = True
        for project, specified_issue_types in issue_types_by_project.items():
            response = self.client.get_project(project)
            all_issue_types_names = set(it["name"] for it in response["issueTypes"])
            unknown = set(specified_issue_types) - all_issue_types_names
            if unknown:
                logger.error(
                    "Jira project %s does not have issue type %s", project, unknown
                )
                success = False
        return success

    def get_issue(self, context: ActionContext, issue_key):
        """Return the Jira issue fields or `None` if not found."""
        try:
            return self.client.get_issue(issue_key)
        except requests_exceptions.HTTPError as exc:
            if exc.response.status_code != 404:
                raise
            logger.error(
                "Could not read issue %s: %s",
                issue_key,
                exc,
                extra=context.model_dump(),
            )
            return None

    def create_jira_issue(
        self, context: ActionContext, description: str, issue_type: str
    ):
        """Create a Jira issue with basic fields in the project and return its key."""
        bug = context.bug
        logger.debug(
            "Create new Jira issue for Bug %s",
            bug.id,
            extra=context.model_dump(),
        )
        fields: dict[str, Any] = {
            "summary": bug.summary,
            "issuetype": {"name": issue_type},
            "description": description[:JIRA_DESCRIPTION_CHAR_LIMIT],
            "project": {"key": context.jira.project},
        }

        jira_response_create = self.client.create_issue(fields=fields)

        # Jira response can be of the form: List or Dictionary
        if isinstance(jira_response_create, list):
            # if a list is returned, get the first item
            jira_response_create = jira_response_create[0]

        if isinstance(jira_response_create, dict):
            # if a dict is returned or the first item in a list, confirm there are no errors
            errs = ",".join(jira_response_create.get("errors", []))
            msgs = ",".join(jira_response_create.get("errorMessages", []))
            if errs or msgs:
                raise JiraCreateError(errs + msgs)

        return jira_response_create

    def add_jira_comment(self, context: ActionContext):
        """Publish a comment on the specified Jira issue"""
        context = context.update(operation=Operation.COMMENT)
        commenter = context.event.user.login if context.event.user else "unknown"
        comment = context.bug.comment
        assert comment  # See jbi.steps.create_comment()

        issue_key = context.jira.issue
        formatted_comment = (
            f"*({commenter})* commented: \n{{quote}}{comment.body}{{quote}}"
        )
        jira_response = self.client.issue_add_comment(
            issue_key=issue_key,
            comment=formatted_comment,
        )
        logger.debug(
            "User comment added to Jira issue %s",
            issue_key,
            extra=context.model_dump(),
        )
        return jira_response

    def add_jira_comments_for_changes(self, context: ActionContext):
        """Add comments on the specified Jira issue for each change of the event"""
        bug = context.bug
        event = context.event
        issue_key = context.jira.issue

        comments: list = []
        user = event.user.login if event.user else "unknown"
        for change in event.changes or []:
            if change.field in ["status", "resolution"]:
                comments.append(
                    {
                        "modified by": user,
                        "resolution": bug.resolution,
                        "status": bug.status,
                    }
                )
            if change.field in ["assigned_to", "assignee"]:
                comments.append({"assignee": bug.assigned_to})

        jira_response_comments = []
        for i, comment in enumerate(comments):
            logger.debug(
                "Create comment #%s on Jira issue %s",
                i + 1,
                issue_key,
                extra=context.update(operation=Operation.COMMENT).model_dump(),
            )
            jira_response = self.client.issue_add_comment(
                issue_key=issue_key, comment=json.dumps(comment, indent=4)
            )
            jira_response_comments.append(jira_response)

        return jira_response_comments

    def delete_jira_issue_if_duplicate(
        self, context: ActionContext, latest_bug: BugzillaBug
    ):
        """Rollback the Jira issue creation if there is already a linked Jira issue
        on the Bugzilla ticket"""
        issue_key = context.jira.issue
        jira_key_in_bugzilla = latest_bug.extract_from_see_also(
            project_key=context.jira.project
        )
        _duplicate_creation_event = (
            jira_key_in_bugzilla is not None and issue_key != jira_key_in_bugzilla
        )
        if not _duplicate_creation_event:
            return None

        logger.warning(
            "Delete duplicated Jira issue %s from Bug %s",
            issue_key,
            context.bug.id,
            extra=context.update(operation=Operation.DELETE).model_dump(),
        )
        jira_response_delete = self.client.delete_issue(issue_id_or_key=issue_key)
        return jira_response_delete

    def add_link_to_bugzilla(self, context: ActionContext):
        """Add link to Bugzilla ticket in Jira issue"""
        bug = context.bug
        issue_key = context.jira.issue
        bugzilla_url = f"{settings.bugzilla_base_url}/show_bug.cgi?id={bug.id}"
        logger.debug(
            "Link %r on Jira issue %s",
            bugzilla_url,
            issue_key,
            extra=context.update(operation=Operation.LINK).model_dump(),
        )
        icon_url = f"{settings.bugzilla_base_url}/favicon.ico"
        return self.client.create_or_update_issue_remote_links(
            issue_key=issue_key,
            link_url=bugzilla_url,
            title=bugzilla_url,
            icon_url=icon_url,
            icon_title=icon_url,
        )

    def clear_assignee(self, context: ActionContext):
        """Clear the assignee of the specified Jira issue."""
        issue_key = context.jira.issue
        logger.debug("Clearing assignee", extra=context.model_dump())
        return self.client.update_issue_field(key=issue_key, fields={"assignee": None})

    def find_jira_user(self, context: ActionContext, email: str):
        """Lookup Jira users, raise an error if not exactly one found."""
        logger.debug("Find Jira user with email %s", email, extra=context.model_dump())
        users = self.client.user_find_by_user_string(query=email)
        if len(users) != 1:
            raise ValueError(f"User {email} not found")
        return users[0]

    def assign_jira_user(self, context: ActionContext, email: str):
        """Set the assignee of the specified Jira issue, raise if fails."""
        issue_key = context.jira.issue
        assert issue_key  # Until we have more fine-grained typing of contexts

        jira_user = self.find_jira_user(context, email)
        jira_user_id = jira_user["accountId"]
        try:
            # There doesn't appear to be an easy way to verify that
            # this user can be assigned to this issue, so just try
            # and do it.
            return self.client.update_issue_field(
                key=issue_key,
                fields={"assignee": {"accountId": jira_user_id}},
            )
        except (requests_exceptions.HTTPError, IOError) as exc:
            raise ValueError(
                f"Could not assign {jira_user_id} to issue {issue_key}"
            ) from exc

    def update_issue_status(self, context: ActionContext, jira_status: str):
        """Update the status of the Jira issue"""
        issue_key = context.jira.issue
        assert issue_key  # Until we have more fine-grained typing of contexts

        logger.debug(
            "Updating Jira status to %s",
            jira_status,
            extra=context.model_dump(),
        )
        return self.client.set_issue_status(
            issue_key,
            jira_status,
        )

    def update_issue_summary(self, context: ActionContext):
        """Update's an issue's summary with the description of an incoming bug"""

        bug = context.bug
        issue_key = context.jira.issue
        logger.debug(
            "Update summary of Jira issue %s for Bug %s",
            issue_key,
            bug.id,
            extra=context.model_dump(),
        )
        truncated_summary = (bug.summary or "")[:JIRA_DESCRIPTION_CHAR_LIMIT]
        fields: dict[str, str] = {
            "summary": truncated_summary,
        }
        jira_response = self.client.update_issue_field(key=issue_key, fields=fields)
        return jira_response

    def update_issue_resolution(self, context: ActionContext, jira_resolution: str):
        """Update the resolution of the Jira issue."""
        issue_key = context.jira.issue
        assert issue_key  # Until we have more fine-grained typing of contexts

        logger.debug(
            "Updating Jira resolution to %s",
            jira_resolution,
            extra=context.model_dump(),
        )
        return self.client.update_issue_field(
            key=issue_key,
            fields={"resolution": jira_resolution},
        )

    def update_issue_components(
        self,
        issue_key: str,
        project: str,
        components: Iterable[str],
    ) -> tuple[Optional[dict], set]:
        """Attempt to add components to the specified issue

        Args:
            issue_key: key of the issues to add the components to
            project: the project key
            components: Component names to add to the issue

        Returns:
            The Jira response (if any), and any components that weren't added
            to the issue because they weren't available on the project
        """
        missing_components = set(components)
        jira_components = []

        all_project_components = self.client.get_project_components(project)
        for comp in all_project_components:
            if comp["name"] in missing_components:
                jira_components.append({"id": comp["id"]})
                missing_components.remove(comp["name"])

        if not jira_components:
            return None, missing_components

        logger.info(
            "attempting to add components '%s' to issue '%s'",
            ",".join(components),
            issue_key,
        )
        resp = self.client.update_issue_field(
            key=issue_key, fields={"components": jira_components}
        )
        return resp, missing_components

    def update_issue_labels(
        self, issue_key: str, add: Iterable[str], remove: Optional[Iterable[str]]
    ):
        """Update the labels for a specified issue

        Args:
            issue_key: key of the issues to modify the labels on
            add: labels to add
            remove (Optional): labels to remove

        Returns:
            The response from Jira
        """
        if not remove:
            remove = []

        updated_labels = [{"add": label} for label in add] + [
            {"remove": label} for label in remove
        ]
        return self.client.update_issue(
            issue_key=issue_key,
            update={"update": {"labels": updated_labels}},
        )


@lru_cache(maxsize=1)
def get_service():
    """Get atlassian Jira Service"""
    client = JiraClient(
        url=settings.jira_base_url,
        username=settings.jira_username,
        password=settings.jira_api_key,  # package calls this param 'password' but actually expects an api key
        cloud=True,  # we run against an instance of Jira cloud
    )

    return JiraService(client=client)
