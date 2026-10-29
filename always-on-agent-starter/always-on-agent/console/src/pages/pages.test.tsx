// One test per page against the fake API (the prototype's data), plus the actions that page offers.
import { describe, expect, it } from "vitest";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { fixture } from "../test/mockApi";
import { renderAt } from "../test/render";

const posts = (calls: { method: string; path: string; body: unknown }[], path: string) =>
  calls.filter((c) => c.method !== "GET" && c.path === path);

describe("shell", () => {
  it("shows the product name, status and counts on Tasks and Approvals", async () => {
    renderAt("#brief");
    expect(screen.getByText("Agent", { selector: "b" })).toBeInTheDocument();
    expect(await screen.findByText("Running · Haiku")).toBeInTheDocument();
    expect(document.getElementById("navTasks")).toHaveTextContent("5");
    expect(document.getElementById("navApprovals")).toHaveTextContent("5");
    expect(screen.getByRole("link", { name: "Morning brief" })).toHaveClass("active");
  });

  it("asks for the one-time link when the session is missing", async () => {
    renderAt("#brief", { "GET /api/status": { status: 401, detail: "open the console with the link" } });
    expect(await screen.findByText("Open the console from its link")).toBeInTheDocument();
  });

  it("shows page not found for unknown routes", async () => {
    renderAt("#nowhere");
    expect(await screen.findByText("Page not found")).toBeInTheDocument();
  });
});

describe("morning brief", () => {
  it("lists what needs the user, the 24-hour clocks with ticks, Power BI and undoable actions", async () => {
    const { calls } = renderAt("#brief");
    expect(await screen.findByText("Good morning, Udit. 5 things need you.")).toBeInTheDocument();
    expect(screen.getByText("Crosses 24 hours at 10:36. Escalation to L2 is drafted.")).toBeInTheDocument();
    const clock = screen.getAllByRole("link").find((a) => a.classList.contains("clock"))!;
    expect(clock.querySelectorAll("i")).toHaveLength(3);
    expect(clock.querySelector(".bar")).toHaveClass("crit");
    expect(screen.getByText("13 / 14")).toBeInTheDocument();
    const undo = screen.getAllByRole("button", { name: "Undo" })[0];
    await userEvent.click(undo);
    await waitFor(() => expect(posts(calls, `/api/activity/${fixture.brief.done[0].audit_id}/undo`)).toHaveLength(1));
  });

  it("runs a check-in and pauses with a toast", async () => {
    const { calls } = renderAt("#brief");
    await userEvent.click(await screen.findByRole("button", { name: "Run a check-in now" }));
    expect(await screen.findByText("Check-in started. Results arrive in your DM.")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Pause agent" }));
    await waitFor(() => expect(posts(calls, "/api/control/pause")).toHaveLength(1));
    expect(await screen.findByText("Paused. Nothing runs until you resume.")).toBeInTheDocument();
  });

  it("shows the paused banner while paused", async () => {
    renderAt("#brief", { "GET /api/status": () => ({ ...fixture.status, paused: true }) });
    expect(await screen.findByText("The agent is paused.")).toBeInTheDocument();
    expect(screen.getByText("Paused · nothing runs")).toBeInTheDocument();
  });
});

describe("tasks", () => {
  it("shows the five columns and filters by project", async () => {
    renderAt("#tasks");
    const needs = await screen.findByTestId("col-needs");
    expect(within(needs).getByText("INC30109981")).toBeInTheDocument();
    for (const c of ["progress", "waiting", "scheduled", "done"]) expect(screen.getByTestId(`col-${c}`)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Power BI" }));
    expect(within(screen.getByTestId("col-needs")).queryByText("INC30109981")).not.toBeInTheDocument();
    expect(within(screen.getByTestId("col-needs")).getByText("REF-0412")).toBeInTheDocument();
  });

  it("shows age bars only for open Doxis tickets", async () => {
    renderAt("#tasks");
    const card = (await screen.findByText("INC30109981")).closest("a")!;
    expect(card.querySelector(".bar")).toHaveClass("crit");
    const pbi = screen.getByText("REF-0412").closest("a")!;
    expect(pbi.querySelector(".bar")).toBeNull();
  });
});

describe("task detail", () => {
  it("shows thread, inline approval, card, summary, checks and precedents", async () => {
    const { calls } = renderAt("#task-inc30114872");
    expect(await screen.findByText("Cannot see documents in project PX-4471")).toBeInTheDocument();
    expect(screen.getByText("Here is the screenshot of Tomás's sites.")).toBeInTheDocument();
    expect(screen.getByText("Member of leading site ORB-02")).toBeInTheDocument();
    expect(screen.getByLabelText("fail")).toBeInTheDocument();
    expect(screen.getByText("INC29874410")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Approve" }));
    await waitFor(() => expect(posts(calls, "/api/approvals/a1/approve")).toHaveLength(1));
    expect(posts(calls, "/api/approvals/a1/approve")[0].body).toEqual({ content_hash: "hash-a1" });
  });

  it("sends a message to the task's agent and marks done", async () => {
    const detail = structuredClone(fixture.task_details.INC30114872);
    const { calls } = renderAt("#task-inc30114872", {
      "GET /api/tasks/INC30114872": () => detail,
      "POST /api/tasks/INC30114872/messages": (body) => {
        const event = { time: "09:01", who: "you", name: "You", text: (body as { text: string }).text };
        detail.events.push(event);
        return event;
      },
      "POST /api/tasks/INC30114872/done": () => ({ ...fixture.tasks[1], column: "done" }),
    });
    await userEvent.type(await screen.findByLabelText("Message this task's agent"), "draft a shorter version");
    await userEvent.click(screen.getByRole("button", { name: "Send" }));
    expect(await screen.findByText("draft a shorter version")).toBeInTheDocument();
    expect(screen.getByText("Agent is working on it…")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Mark done" }));
    await waitFor(() => expect(posts(calls, "/api/tasks/INC30114872/done")).toHaveLength(1));
    expect(await screen.findByText("INC30114872 marked done.")).toBeInTheDocument();
  });

  it("shows not found for an unknown task", async () => {
    renderAt("#task-inc00000000");
    expect(await screen.findByText("Page not found")).toBeInTheDocument();
  });
});

describe("approvals", () => {
  it("approves with the hash shown, edits into a new version, rejects", async () => {
    const { calls } = renderAt("#approvals");
    const card = await screen.findByTestId("approval-a2");
    await userEvent.click(within(card).getByRole("button", { name: "Edit" }));
    const box = within(card).getByLabelText("Edit the text");
    await userEvent.clear(box);
    await userEvent.type(box, "Shorter text.");
    await userEvent.click(within(card).getByRole("button", { name: "Save edit" }));
    await waitFor(() => expect(posts(calls, "/api/approvals/a2/edit")).toHaveLength(1));
    expect(posts(calls, "/api/approvals/a2/edit")[0].body).toEqual({ text: "Shorter text.", content_hash: "hash-a2" });
    expect(await screen.findByText("Edit saved. Approve the edited text to send it.")).toBeInTheDocument();

    await userEvent.click(within(screen.getByTestId("approval-a1")).getByRole("button", { name: "Reject" }));
    await waitFor(() => expect(posts(calls, "/api/approvals/a1/reject")).toHaveLength(1));
  });

  it("explains a stale approval instead of sending", async () => {
    renderAt("#approvals", {
      "POST /api/approvals/a2/approve": { status: 409, detail: "the text changed since this card was shown; approve the new version" },
    });
    const card = await screen.findByTestId("approval-a2");
    await userEvent.click(within(card).getByRole("button", { name: "Approve" }));
    expect(await screen.findByText("the text changed since this card was shown; approve the new version")).toBeInTheDocument();
  });

  it("says all clear when nothing waits", async () => {
    renderAt("#approvals", { "GET /api/approvals": () => [] });
    expect(await screen.findByText("All clear")).toBeInTheDocument();
  });
});

describe("recipes", () => {
  it("groups recipes and changes status", async () => {
    const { calls } = renderAt("#recipes", {
      "POST /api/recipes/inbox-triage/status": (body) => ({ ...fixture.recipes[1], status: (body as { status: string }).status }),
    });
    expect(await screen.findByText("Phase 1 skills")).toBeInTheDocument();
    expect(screen.getByText("Doxis recipes")).toBeInTheDocument();
    await userEvent.selectOptions(screen.getByLabelText("Status of Inbox triage"), "Off");
    await waitFor(() => expect(posts(calls, "/api/recipes/inbox-triage/status")).toHaveLength(1));
    expect(await screen.findByText("Inbox triage is now Off.")).toBeInTheDocument();
  });

  it("edits a recipe with Hand off tools disabled and a live YAML preview, saves in Shadow", async () => {
    const { calls } = renderAt("#recipe-new", {
      "PUT /api/recipes/weekly-ticket-digest": (body) => ({ ...fixture.recipes[0], ...(body as object), slug: "weekly-ticket-digest", status: "Shadow" }),
    });
    await userEvent.type(await screen.findByLabelText("Name"), "Weekly ticket digest");
    expect(screen.getByText(/recipes\/weekly-ticket-digest\.yaml/)).toBeInTheDocument();
    const deleteBox = document.querySelector('input[data-tool="delete_item"]') as HTMLInputElement;
    expect(deleteBox).toBeDisabled();
    await userEvent.click(document.querySelector('input[data-tool="send_mail"]')!);
    expect(document.getElementById("yaml")!.textContent).toContain("- send_mail   # Ask");
    await userEvent.click(screen.getByRole("button", { name: "Save in Shadow mode" }));
    await waitFor(() => expect(posts(calls, "/api/recipes/weekly-ticket-digest")).toHaveLength(1));
    expect((posts(calls, "/api/recipes/weekly-ticket-digest")[0].body as { tools: string[] }).tools).toContain("send_mail");
  });

  it("tests a saved recipe on a sample", async () => {
    renderAt("#recipe-morning-brief", {
      "POST /api/recipes/morning-brief/test": () => ({
        sample: "a scheduled run on the sample day",
        proposals: [{ tool: "publish_brief", mode: "Silent", note: "brief" }],
        notes: ["Shadow run on sample data: nothing was sent or changed."],
        needs_approval: false,
      }),
    });
    await userEvent.click(await screen.findByRole("button", { name: "Test on a sample" }));
    expect(await screen.findByText("Test run on a sample")).toBeInTheDocument();
    expect(screen.getByText("No approval needed.")).toBeInTheDocument();
  });
});

describe("activity", () => {
  it("shows workers, filters by mode and undoes a Report action", async () => {
    const { calls } = renderAt("#activity");
    expect(await screen.findByText("INC30117002 · precedent search")).toBeInTheDocument();
    expect(await screen.findByText("Moved 9 notification emails to Filed/Notifications")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Hand off" }));
    expect(await screen.findByText("Stopped a reply to an address outside the company")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "All" }));
    await userEvent.click((await screen.findAllByRole("button", { name: "Undo" }))[0]);
    await waitFor(() => expect(calls.some((c) => c.method === "POST" && c.path.endsWith("/undo"))).toBe(true));
  });
});

describe("memory", () => {
  it("has the five tabs, stale flags and lesson decisions", async () => {
    const { calls } = renderAt("#memory", {
      "POST /api/lessons/l1": () => ({ id: "l1", status: "approved" }),
    });
    expect(await screen.findByText(/Udit · App developer/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("tab", { name: "Site directory" }));
    expect(await screen.findByText("Stale")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("tab", { name: "Lessons" }));
    await userEvent.click((await screen.findAllByRole("button", { name: "Save rule" }))[0]);
    await waitFor(() => expect(posts(calls, "/api/lessons/l1")).toHaveLength(1));
    await userEvent.click(screen.getByRole("tab", { name: "Reference sources" }));
    expect(await screen.findByText("Doxis incident workbook (L2 history)")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("tab", { name: "Task memory" }));
    expect(screen.getByText("How each task remembers")).toBeInTheDocument();
  });
});

describe("connectors", () => {
  it("shows a tile per adapter with what it can do", async () => {
    renderAt("#connectors");
    expect(await screen.findByText("Outlook mail")).toBeInTheDocument();
    expect(screen.getByText("Read and send")).toHaveClass("st", "part");
    expect(screen.getByText("Send only after approval (Ask)")).toBeInTheDocument();
  });
});

describe("settings", () => {
  it("locks Hand off rows, keeps the data rule read-only and saves a policy row", async () => {
    const { calls } = renderAt("#prefs");
    expect(await screen.findByText("Waiting for written approval")).toBeInTheDocument();
    expect(screen.getByLabelText("Mode for Send an email or chat message")).toBeDisabled();
    expect(screen.queryByLabelText("Mode for Delete anything")).toBeNull();
    for (const box of screen.getAllByRole("checkbox", { name: /Metadata|Ticket|Chat|Screenshots/ })) expect(box).toBeDisabled();
    await userEvent.selectOptions(screen.getByLabelText("Mode for File or flag your own mail"), "Ask");
    await waitFor(() => expect(posts(calls, "/api/settings")).toHaveLength(1));
    expect(posts(calls, "/api/settings")[0].body).toEqual({ policy: { file_mail: "Ask" } });
    expect(JSON.stringify(posts(calls, "/api/settings")[0].body)).not.toContain("data_rule");
  });

  it("asks to pause before a reset and confirms in the page", async () => {
    renderAt("#prefs");
    await userEvent.click(await screen.findByRole("button", { name: "Reset memory and tasks" }));
    expect(await screen.findByText("Pause the agent before a reset.")).toBeInTheDocument();
    expect(screen.queryByText("Delete and reset")).toBeNull();
  });

  it("resets with the confirmation token when paused", async () => {
    const { calls } = renderAt("#prefs", {
      "GET /api/status": () => ({ ...fixture.status, paused: true }),
      "POST /api/control/reset/request": () => ({ token: "tok-123", expires_minutes: 10 }),
      "POST /api/control/reset": () => ({ paused: true, message: "Reset done: 3 tasks and their memory deleted. The audit log is kept." }),
    });
    await screen.findByText("The agent is paused.");
    await userEvent.click(screen.getByRole("button", { name: "Reset memory and tasks" }));
    await userEvent.click(await screen.findByRole("button", { name: "Delete and reset" }));
    await waitFor(() => expect(posts(calls, "/api/control/reset")).toHaveLength(1));
    expect(posts(calls, "/api/control/reset")[0].body).toEqual({ token: "tok-123" });
  });
});
