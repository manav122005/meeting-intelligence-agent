import os
import json
from mcp.server.fastmcp import FastMCP

# Initialize FastMCP Server
mcp = FastMCP("Meeting Intelligence Server")

# Mock database of user emails
MOCK_DIRECTORY = {
    "alice smith": "alice.smith@enterprise.com",
    "bob jones": "bob.jones@enterprise.com",
    "charlie brown": "charlie.brown@enterprise.com",
    "diana prince": "diana.prince@enterprise.com",
    "evan wright": "evan.wright@enterprise.com",
}

# Mock calendar database
MOCK_CALENDAR = {
    "2026-07-03": [
        {"time": "09:00 AM", "title": "Project Alignment & Roadmap Review", "organizer": "Alice Smith"},
        {"time": "11:00 AM", "title": "Security Checkpoint Audit", "organizer": "Bob Jones"},
        {"time": "02:00 PM", "title": "Sprint Planning", "organizer": "Charlie Brown"},
    ]
}


@mcp.tool()
def get_calendar_events(date: str) -> str:
    """
    List calendar events for a given date (format: YYYY-MM-DD).
    Useful to lookup meeting context and details.
    """
    events = MOCK_CALENDAR.get(date, [])
    if not events:
        return f"No events found for {date}."
    return json.dumps(events, indent=2)


@mcp.tool()
def get_user_email(name: str) -> str:
    """
    Look up a team member's email address by name.
    Useful to verify action item owner contact details for follow-up notifications.
    """
    normalized_name = name.strip().lower()
    email = MOCK_DIRECTORY.get(normalized_name)
    if email:
        return f"User: {name.strip()} | Email: {email}"
    
    # Try fuzzy substring match
    for mock_name, mock_email in MOCK_DIRECTORY.items():
        if normalized_name in mock_name:
            return f"User: {mock_name.title()} | Email: {mock_email}"
            
    return f"User '{name}' not found in organization directory."


@mcp.tool()
def create_task(title: str, description: str, assignee: str, deadline: str) -> str:
    """
    Simulate creating a task/issue in the team tracking system (e.g. Jira/Trello).
    """
    # Create the task description
    task_info = {
        "status": "CREATED",
        "task_id": f"TASK-{hash(title) % 10000:04d}",
        "title": title,
        "description": description,
        "assignee": assignee,
        "deadline": deadline
    }
    return f"Successfully created ticket:\n{json.dumps(task_info, indent=2)}"


@mcp.tool()
def save_summary(title: str, content: str) -> str:
    """
    Save meeting summary/action items report to the archive file.
    """
    safe_title = "".join(c for c in title if c.isalnum() or c in (" ", "-", "_")).strip()
    filename = f"summary_{safe_title.replace(' ', '_').lower()}.md"
    
    # Create output directory if needed
    os.makedirs("artifacts/summaries", exist_ok=True)
    filepath = os.path.join("artifacts/summaries", filename)
    
    try:
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(content)
        return f"Meeting summary successfully saved to: {filepath}"
    except Exception as e:
        return f"Error saving meeting summary: {str(e)}"


if __name__ == "__main__":
    mcp.run()
