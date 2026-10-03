import { routeFetch } from "../test/fakes";
import { api, ApiProblem, REAUTHENTICATION_REQUIRED, reauthenticationUrl, setCsrfToken, unwrap } from "./client";

afterEach(() => {
  vi.unstubAllGlobals();
  setCsrfToken(null);
});

describe("API client", () => {
  it("sends the CSRF token on unsafe requests only", async () => {
    const calls = routeFetch({
      "GET /api/homes": { status: 200, body: [] },
      "POST /api/homes": { status: 201, body: { id: "h1" } },
    });
    setCsrfToken("csrf-1");

    await unwrap(api.GET("/homes"));
    await unwrap(api.POST("/homes", { body: { name: "Casa" } }));

    expect(calls[0]?.headers.get("x-csrf-token")).toBeNull();
    expect(calls[1]?.headers.get("x-csrf-token")).toBe("csrf-1");
  });

  it("turns problem details into an ApiProblem", async () => {
    routeFetch({
      "POST /api/homes/h1/automations": {
        status: 422,
        body: { type: "urn:smarthome:problem:invalid-automation", title: "Invalid definition", status: 422, path: "triggers/0" },
      },
    });

    const failure = await unwrap(
      api.POST("/homes/{home_id}/automations", {
        params: { path: { home_id: "h1" } },
        body: { name: "x", definition: {} },
      }),
    ).catch((error: unknown) => error);

    expect(failure).toBeInstanceOf(ApiProblem);
    expect((failure as ApiProblem).problem.path).toBe("triggers/0");
  });

  it("builds the sign-in-again link for step-up problems only", () => {
    const problem = {
      type: REAUTHENTICATION_REQUIRED,
      title: "Sign in again",
      status: 401,
      login_url: "/api/auth/login?reauth=true",
    };

    expect(reauthenticationUrl(problem, "/homes/h1")).toBe(
      "/api/auth/login?reauth=true&return_to=%2Fhomes%2Fh1",
    );
    expect(reauthenticationUrl({ ...problem, type: "about:blank" }, "/")).toBeNull();
  });
});
