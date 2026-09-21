"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { getSystemStatus } from "@/lib/api";
import type { SystemStatus } from "@/lib/types";
import { useApiResource } from "@/lib/useApiResource";

/**
 * Primary navigation.
 *
 * Sections that do not exist yet are rendered as disabled items tagged
 * PLANNED, not as links to empty pages. Showing them keeps the roadmap honest
 * and visible; linking them would be a promise the build cannot keep.
 *
 * Testnet is the one item whose availability is not a property of this
 * codebase: it depends on whether *the backend this browser is talking to* has
 * credentials and a durable order store. So it is not hardcoded either way.
 * The mode list is read from the server, and until it answers the item is
 * shown as unavailable rather than as a link -- offering a control the server
 * would refuse is how a UI teaches users that it lies.
 */

const BUILT = [
  { href: "/markets", label: "Markets" },
  { href: "/scanner", label: "Scanner" },
  { href: "/backtest", label: "Backtest" },
  { href: "/paper", label: "Paper Trading" },
] as const;

/** Phase in which each section is scheduled — shown so the gap is explicit. */
const PLANNED = [
  { label: "Falcon", phase: 9 },
  { label: "Watchlist", phase: 3 },
  { label: "Account", phase: 1 },
] as const;

/** Read once per mount. Mode posture changes with a deployment, not a minute. */
function useTestnetEnabled(): { enabled: boolean; known: boolean } {
  const { state } = useApiResource<SystemStatus>(
    (signal) => getSystemStatus(signal),
    [],
  );
  if (state.kind !== "success") return { enabled: false, known: false };
  const testnet = state.data.modes.find((m) => m.mode === "TESTNET");
  return { enabled: testnet?.enabled === true, known: true };
}

export function Nav() {
  const pathname = usePathname();
  const { enabled, known } = useTestnetEnabled();

  return (
    <nav className="nav" aria-label="Primary">
      {BUILT.map((item) => (
        <Link
          key={item.href}
          href={item.href}
          aria-current={pathname.startsWith(item.href) ? "page" : undefined}
        >
          {item.label}
        </Link>
      ))}

      {enabled ? (
        <Link
          href="/testnet"
          aria-current={pathname.startsWith("/testnet") ? "page" : undefined}
        >
          Testnet / Demo
          <span className="tag tag-demo">DEMO</span>
        </Link>
      ) : (
        <span
          className="disabled"
          aria-disabled="true"
          title={
            known
              ? "The backend reports testnet execution as not enabled."
              : "Waiting for the backend to report which trading modes are available."
          }
        >
          Testnet / Demo
          <span className="tag">{known ? "UNAVAILABLE" : "CHECKING"}</span>
        </span>
      )}

      {PLANNED.map((item) => (
        <span
          key={item.label}
          className="disabled"
          aria-disabled="true"
          title={`Not implemented. Scheduled for phase ${item.phase}.`}
        >
          {item.label}
          <span className="tag">PLANNED</span>
        </span>
      ))}

      {/* LIVE is not a navigation item and deliberately has no route. It is
          shown as a locked posture indicator so its absence is visible rather
          than merely unmentioned -- a mode nobody can see is a mode nobody can
          verify is off. */}
      <span className="disabled nav-live" aria-disabled="true" title="Live trading is not implemented.">
        Live
        <span className="tag tag-locked">LOCKED</span>
      </span>
    </nav>
  );
}
