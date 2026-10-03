import { bars } from "./bars";

describe("bar geometry", () => {
  it("scales bars to the largest value and stands them on the baseline", () => {
    const [a, b] = bars([5, 10], 100, 50, 0);
    expect(a).toEqual({ x: 0, y: 25, width: 50, height: 25 });
    expect(b).toEqual({ x: 50, y: 0, width: 50, height: 50 });
  });

  it("draws nothing without values and flat bars when everything is zero", () => {
    expect(bars([], 100, 50)).toEqual([]);
    expect(bars([0, 0], 100, 50).map((bar) => bar.height)).toEqual([0, 0]);
  });
});
