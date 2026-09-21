import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { EquityCurve } from "./EquityCurve";
import type { EquityPoint } from "@/lib/types";

function point(index: number, equity: number, drawdown = 0): EquityPoint {
  return {
    time: new Date(Date.UTC(2026, 0, 1, index)).toISOString(),
    equity: equity.toFixed(8),
    drawdown_percent: drawdown.toFixed(4),
    in_position: index % 2 === 0,
  };
}

describe("EquityCurve", () => {
  it("draws the curve as a small number of paths", () => {
    // One path per pane regardless of length: a 500-point curve must not
    // become 500 DOM nodes.
    const points = Array.from({ length: 300 }, (_, i) => point(i, 100 + i * 0.1, i % 7));
    const { container } = render(<EquityCurve points={points} startingBalance="100" />);
    expect(container.querySelectorAll("path")).toHaveLength(2);
  });

  it("describes itself for assistive technology", () => {
    const points = [point(0, 100), point(1, 101), point(2, 103.5)];
    render(<EquityCurve points={points} startingBalance="100" />);
    expect(
      screen.getByRole("img", { name: /equity curve over 3 points, ending at 103.50/ }),
    ).toBeInTheDocument();
  });

  it("marks the starting balance, the line the result is judged against", () => {
    render(<EquityCurve points={[point(0, 100), point(1, 99), point(2, 98)]} startingBalance="100" />);
    expect(screen.getByText("start")).toBeInTheDocument();
  });

  it("colours a losing curve differently from a winning one", () => {
    const losing = render(
      <EquityCurve points={[point(0, 100), point(1, 98), point(2, 95)]} startingBalance="100" />,
    );
    const equityPath = losing.container.querySelector("path");
    expect(equityPath).toHaveAttribute("stroke", "#e0554e");

    const winning = render(
      <EquityCurve points={[point(0, 100), point(1, 102), point(2, 105)]} startingBalance="100" />,
    );
    expect(winning.container.querySelectorAll("path")[0]).toHaveAttribute("stroke", "#26a96a");
  });

  it("survives a perfectly flat curve without dividing by zero", () => {
    const flat = Array.from({ length: 10 }, (_, i) => point(i, 100));
    const { container } = render(<EquityCurve points={flat} startingBalance="100" />);
    for (const path of container.querySelectorAll("path")) {
      expect(path.getAttribute("d")).not.toContain("NaN");
    }
  });

  it("says so when there is nothing to plot", () => {
    render(<EquityCurve points={[point(0, 100)]} startingBalance="100" />);
    expect(screen.getByText("NO CURVE")).toBeInTheDocument();
  });

  it("reports the worst drawdown reached", () => {
    const points = [point(0, 100, 0), point(1, 97, 3), point(2, 99, 1)];
    render(<EquityCurve points={points} startingBalance="100" />);
    expect(screen.getByText("3.00%")).toBeInTheDocument();
  });
});
