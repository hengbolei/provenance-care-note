# Deploying the public demo to Hugging Face Spaces

The Space holds only `Dockerfile` and `README.md` from this folder. Its build clones this
repository at `REF` (default `main`), installs it and runs it with `CARENOTE_PUBLIC_DEMO=1`:

- the in-memory store with the synthetic seed, reset to the seed every 30 minutes;
- the review agent on `recorded`, replaying `evals/cassettes/`, so no API key is needed;
- a banner that says the demo is shared and synthetic, and session cookies marked `Secure`.

The app refuses to start in public-demo mode with a database or in the production environment.

## First deployment

1. Merge the public-demo code into `main`: the Space builds `main`.
2. On Hugging Face, create a Space: SDK **Docker**, template **Blank**, hardware **CPU basic
   (free)**, visibility **Public**.
3. In the Space's **Files** tab, choose **Add file → Upload files** and upload `Dockerfile` and
   `README.md` from this folder, replacing the generated README. The build starts on commit.
4. When the Space shows **Running**, open it through **⋮ → Embed this Space → Direct URL**
   (`https://<user>-<space>.hf.space`). Sign in there: the session cookie is `SameSite=Strict`,
   so it is not sent inside the embedded view on huggingface.co, which shows a link to open the
   demo in its own tab instead.

## Updating

The Space does not follow `main` by itself. After changes reach `main`, use **Settings → Factory
rebuild** to clone and build again. To pin a version, change `ARG REF` to a tag or branch.

A free Space sleeps after a period without visitors and wakes on the next visit, which takes
about a minute; its state starts from the seed again.
