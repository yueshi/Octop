import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import TurnInProgressNotice from "./TurnInProgressNotice";

describe("TurnInProgressNotice", () => {
  it("shows elapsed time, queued count, and a stop action", () => {
    const onStop = vi.fn();
    render(
      <TurnInProgressNotice
        startedAt={Date.now() - 12_000}
        queuedCount={2}
        onStop={onStop}
      />,
    );

    expect(screen.getByText("This turn is still running")).toBeInTheDocument();
    expect(screen.getByText(/Waiting 12s/)).toBeInTheDocument();
    expect(screen.getByText(/2 queued/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Stop this turn" }));
    expect(onStop).toHaveBeenCalledTimes(1);
  });

  it("uses the member-only copy without a stop action", () => {
    render(<TurnInProgressNotice membersOnly />);

    expect(
      screen.getByText("Team members are still replying"),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Stop this turn" }),
    ).not.toBeInTheDocument();
  });
});
