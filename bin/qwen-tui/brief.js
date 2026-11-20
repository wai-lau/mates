// mates: OpenCode's side panel shows either Qwen's todo list or a short brief, never both (Wai,
// 2026-11-18: "show the overall plan and/or the instigating prompt in that Qwen panel", then
// "show TODO expanded OR short brief, not both / todo > brief"), right under the panel's title
// (Wai, 2026-11-20: "move todo in the qwen right pane right under title"): OpenCode's own todo
// section sits below Context and LSP, so tui.json turns it off and this draws the list, expanded,
// while any item is open, the same rule OpenCode's used. Until then it shows the first line of the lead's
// brief, which mates writes to a folder bin/qwen mounts read-only at /opt/mates-brief, so Qwen
// cannot change what the panel says it was asked. Re-checked every few seconds. Anything
// unexpected (a newer OpenCode without this API) leaves the panel as it was.
import { BoxRenderable, TextRenderable } from "@opentui/core"
import { readFileSync } from "node:fs"

const FILE = "/opt/mates-brief/brief.md"
const MAX_CHARS = 160

function shortBrief() {
  try {
    const first = readFileSync(FILE, "utf8").split("\n").find((l) => l.trim()) ?? ""
    const line = first.trim()
    return line.length > MAX_CHARS ? line.slice(0, MAX_CHARS - 1) + "…" : line
  } catch {
    return ""
  }
}

const MARK = { completed: "[✓]", in_progress: "[•]", cancelled: "[-]" }

function openTodos(api, sessionID) {
  // OpenCode's rule for showing its todo section: some item not completed
  try {
    const todos = api.state.session.todo(sessionID) ?? []
    return todos.length > 0 && todos.some((t) => t.status !== "completed") ? todos : []
  } catch {
    return []
  }
}

export default {
  id: "mates-brief",
  tui: async (api) => {
    api.slots.register({
      order: 50,
      slots: {
        sidebar_content(_ctx, props) {
          try {
            const theme = api.theme?.current ?? {}
            const box = new BoxRenderable(api.renderer, { flexDirection: "column", flexShrink: 0 })
            const head = new TextRenderable(api.renderer, { content: "", fg: theme.text })
            const body = new TextRenderable(api.renderer, { content: "", fg: theme.textMuted, wrapMode: "word" })
            box.add(head)
            box.add(body)
            const update = () => {
              try {
                const todos = openTodos(api, props?.session_id)
                const text = todos.length
                  ? todos.map((t) => `${MARK[t.status] ?? "[ ]"} ${t.content}`).join("\n")
                  : shortBrief()
                const title = todos.length ? "Todo" : text ? "Task (from the lead)" : ""
                if (head.content !== title) head.content = title
                if (body.content !== text) body.content = text
                box.visible = Boolean(text)
              } catch {}
            }
            update()
            const timer = setInterval(update, 3000)
            timer.unref?.()
            return box
          } catch {
            return undefined
          }
        },
      },
    })
  },
}
