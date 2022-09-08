"""
Default action is listed below.
`init` is required; and requires at minimum the `jira_project_key` parameter.
The `label_field` parameter configures which Jira field is used to store the
labels generated from the Bugzilla status whiteboard.

`init` should return a __call__able
"""
import logging

from jbi import ActionResult, Operation
from jbi.environment import get_settings
from jbi.models import ActionContext, BugzillaBug, BugzillaWebhookEvent, JiraContext
from jbi.services import bugzilla, jira

settings = get_settings()

logger = logging.getLogger(__name__)

JIRA_REQUIRED_PERMISSIONS = {
    "ADD_COMMENTS",
    "CREATE_ISSUES",
    "DELETE_ISSUES",
    "EDIT_ISSUES",
}


def init(jira_project_key, sync_whiteboard_labels=True, **kwargs):
    """Function that takes required and optional params and returns a callable object"""
    return DefaultExecutor(
        jira_project_key=jira_project_key,
        sync_whiteboard_labels=sync_whiteboard_labels,
        **kwargs,
    )


class DefaultExecutor:
    """Callable class that encapsulates the default action."""

    def __init__(self, jira_project_key, **kwargs):
        """Initialize DefaultExecutor Object"""
        self.jira_project_key = jira_project_key
        self.sync_whiteboard_labels = kwargs.get("sync_whiteboard_labels", True)

    def __call__(self, bug: BugzillaBug, event: BugzillaWebhookEvent) -> ActionResult:
        """Called from BZ webhook when default action is used. All default-action webhook-events are processed here."""
        linked_issue_key = bug.extract_from_see_also()

        context = ActionContext(
            event=event,
            bug=bug,
            operation=Operation.IGNORE,
            jira=JiraContext(
                issue=linked_issue_key,
                project=self.jira_project_key,
            ),
        )

        parameters = {
            "jira_project_key": self.jira_project_key,
            "sync_whiteboard_labels": self.sync_whiteboard_labels,
        }

        context, comment_responses = maybe_create_comment(context=context, **parameters)
        context, create_responses = maybe_create_issue(context=context, **parameters)
        context, update_responses = maybe_update_issue(context=context, **parameters)

        context, changes_responses = maybe_add_jira_comments_for_changes(
            context=context, **parameters
        )

        is_noop = context.operation == Operation.IGNORE
        if is_noop:
            logger.debug(
                "Ignore event target %r",
                event.target,
                extra=context.dict(),
            )

        return not is_noop, {
            "responses": comment_responses
            + create_responses
            + update_responses
            + changes_responses
        }


def maybe_create_comment(context: ActionContext, **parameters):
    """Create a Jira comment if event is `"comment"`"""
    event = context.event
    bug = context.bug
    linked_issue_key = context.jira.issue

    if event.target != "comment" or not linked_issue_key:
        return context, ()

    if bug.comment is None:
        logger.debug(
            "No matching comment found in payload",
            extra=context.dict(),
        )
        return context, ()

    context = context.update(operation=Operation.COMMENT)
    commenter = event.user.login if event.user else "unknown"
    jira_response = jira.add_jira_comment(
        context, linked_issue_key, commenter, bug.comment
    )
    return context, (jira_response,)


def maybe_create_issue(
    context: ActionContext, **parameters
):  # pylint: disable=too-many-arguments
    """Create Jira issue and establish link between bug and issue; rollback/delete if required"""
    sync_whiteboard_labels: bool = parameters["sync_whiteboard_labels"]
    event = context.event
    bug = context.bug
    jira_project_key = context.jira.project
    linked_issue_key = context.jira.issue

    if (
        event.target != "bug"
        or linked_issue_key
        or context.operation != Operation.IGNORE
    ):
        return context, ()

    context = context.update(operation=Operation.CREATE)

    # In the payload of a bug creation, the `comment` field is `null`.
    # We fetch the list of comments to use the first one as the Jira issue description.
    comment_list = bugzilla.get_client().get_comments(bug.id)
    description = comment_list[0].text if comment_list else ""

    issue_key = jira.create_jira_issue(
        context,
        bug,
        description,
        jira_project_key,
        sync_whiteboard_labels=sync_whiteboard_labels,
    )

    context.jira.issue = issue_key

    bug = bugzilla.get_client().get_bug(bug.id)
    jira_response_delete = jira.delete_jira_issue_if_duplicate(context, bug, issue_key)
    if jira_response_delete:
        return context, (jira_response_delete,)

    bugzilla_response = bugzilla.add_link_to_jira(context, bug, issue_key)

    jira_response = jira.add_link_to_bugzilla(context, issue_key, bug)

    return context, (bugzilla_response, jira_response)


def maybe_update_issue(context: ActionContext, **parameters):
    """Update the Jira issue if bug with linked issue is modified."""
    sync_whiteboard_labels: bool = parameters["sync_whiteboard_labels"]
    event = context.event
    bug = context.bug
    linked_issue_key = context.jira.issue

    if (
        event.target != "bug"
        or not linked_issue_key
        or context.operation != Operation.IGNORE
    ):
        return context, ()

    changed_fields = event.changed_fields() or []
    context = context.update(
        operation=Operation.UPDATE,
        extra={
            "changed_fields": ", ".join(changed_fields),
        },
    )
    resp = jira.update_jira_issue(
        context, bug, linked_issue_key, sync_whiteboard_labels
    )

    return context, (resp,)


def maybe_add_jira_comments_for_changes(context: ActionContext, **parameters):
    """Add a Jira comment for each field change on Bugzilla"""
    if context.operation != Operation.UPDATE:
        return context, ()

    assert context.jira.issue  # Until we have more fine-grained typing of contexts

    comments_responses = jira.add_jira_comments_for_changes(
        context=context,
        event=context.event,
        bug=context.bug,
        linked_issue_key=context.jira.issue,
    )
    return context, tuple(comments_responses)
