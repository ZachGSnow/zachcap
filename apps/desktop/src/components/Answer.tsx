import { toSeconds } from "@/lib/format";

export type CitedMeetings = Record<string, { title: string; started_at: string }>;

/** Where a citation points: a moment in this meeting, or (with an id) in another one. */
export type Cite = (meetingId: number | null, seconds: number | null) => void;

// Meeting ids are capped at 9 digits, like the engine's, so a garbled id stays plain text.
const TOKEN = /(\*\*[^*]+\*\*|\[M\d{1,9}(?:\s+\d{1,2}:\d{2}(?::\d{2})?)?\]|\[\d{1,2}:\d{2}(?::\d{2})?\])/g;

type InlineProps = { text: string; meetings?: CitedMeetings; localTimes: boolean; onCite: Cite };

/** One line of an answer: **bold**, [12:34] moments in this meeting, and [M12 04:31]
 * moments in any meeting, shown with that meeting's title. */
function Inline({ text, meetings, localTimes, onCite }: InlineProps) {
  return (
    <>
      {text.split(TOKEN).map((part, index) => {
        if (part.startsWith("**") && part.endsWith("**") && part.length > 4) {
          return <strong key={index}>{part.slice(2, -2)}</strong>;
        }
        const other = /^\[M(\d{1,9})(?:\s+([\d:]+))?\]$/.exec(part);
        if (other) {
          const meetingId = Number(other[1]);
          const at = other[2] ? toSeconds(other[2]) : null;
          const title = meetings?.[meetingId]?.title ?? `Meeting ${meetingId}`;
          return (
            <button
              key={index}
              onClick={() => onCite(meetingId, at)}
              title={`Open ${title}${other[2] ? ` at ${other[2]}` : ""}`}
              className="mx-0.5 inline-flex max-w-56 items-baseline gap-1 rounded bg-muted px-1 align-baseline text-xs text-foreground hover:bg-foreground hover:text-background"
            >
              <span className="truncate">{title}</span>
              {other[2] && <span className="font-mono">{other[2]}</span>}
            </button>
          );
        }
        const at = /^\[\d/.test(part) ? toSeconds(part) : null;
        if (at != null && !localTimes) {
          return (
            <span key={index} className="mx-0.5 font-mono text-xs text-muted-foreground">
              {part.slice(1, -1)}
            </span>
          );
        }
        return at != null ? (
          <button
            key={index}
            onClick={() => onCite(null, at)}
            className="mx-0.5 rounded bg-muted px-1 font-mono text-xs text-foreground hover:bg-foreground hover:text-background"
          >
            {part.slice(1, -1)}
          </button>
        ) : (
          <span key={index}>{part}</span>
        );
      })}
    </>
  );
}

/** Light Markdown from Claude: paragraphs and "-" bullet lists, with inline bold and citations.
 * localTimes: whether a bare [12:34] is a moment of the meeting on screen (false on the Ask
 * page, where it names no meeting and is shown as plain text). */
export function Answer({
  text,
  meetings,
  localTimes = true,
  onCite,
}: {
  text: string;
  meetings?: CitedMeetings;
  localTimes?: boolean;
  onCite: Cite;
}) {
  const blocks: { list: boolean; lines: string[] }[] = [];
  for (const raw of text.split("\n")) {
    const line = raw.trimEnd();
    const bullet = /^\s*[-*] /.test(line);
    if (!line.trim()) {
      blocks.push({ list: false, lines: [] });
      continue;
    }
    const last = blocks.at(-1);
    const content = bullet ? line.replace(/^\s*[-*] /, "") : line.replace(/^#+\s+/, "");
    if (last && last.list === bullet && (bullet || last.lines.length)) last.lines.push(content);
    else blocks.push({ list: bullet, lines: [content] });
  }
  return (
    <div className="space-y-2 text-sm leading-relaxed">
      {blocks
        .filter((block) => block.lines.length)
        .map((block, index) =>
          block.list ? (
            <ul key={index} className="list-disc space-y-1 pl-5">
              {block.lines.map((line, i) => (
                <li key={i}>
                  <Inline text={line} meetings={meetings} localTimes={localTimes} onCite={onCite} />
                </li>
              ))}
            </ul>
          ) : (
            <p key={index}>
              {block.lines.map((line, i) => (
                <span key={i}>
                  {i > 0 && <br />}
                  <Inline text={line} meetings={meetings} localTimes={localTimes} onCite={onCite} />
                </span>
              ))}
            </p>
          ),
        )}
    </div>
  );
}
