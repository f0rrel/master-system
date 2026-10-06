# GitHub setup

Connects one managed project's repository to Master System. It takes about 15 minutes in
the browser plus three commands. When done:

- the system has its own GitHub identity, a **GitHub App** that can access only your repo;
- it can push `develop` and the preview site, but **can never change `main`**;
- if the project has a preview site (`github.site_dir`), the preview of your app is
  served at `https://<owner>.github.io/<repo>/develop/` and the released version at
  `https://<owner>.github.io/<repo>/`.

`ms github check` reports which steps are complete at any time. Its messages refer to the
step numbers below.

Examples use the repository `<you>/example-app`; substitute your own.

---

## 1. Why a GitHub App

A GitHub App is a separate identity that you control:

- You choose exactly which repositories it can access and what it may do.
- Its tokens last one hour and are created on demand from a private key kept on your
  machine.
- Uninstalling the App revokes all access immediately.

Personal access tokens or your own credentials would give the system far more authority
than it needs, including the ability to change `main`.

## 2. Create the App

1. Open <https://github.com/settings/apps/new> (profile picture → **Settings** →
   **Developer settings** → **GitHub Apps** → **New GitHub App**).
2. Fill in the form; leave every field not listed here at its default.

   | Field | Value |
   | --- | --- |
   | **GitHub App name** | Any unique name, e.g. `<you>-master-system` |
   | **Homepage URL** | Your fork of this repository, or any URL you own |
   | **Webhook → Active** | **Unticked.** The system polls GitHub; nothing calls it. |

3. Under **Permissions → Repository permissions**, set exactly these. Everything else stays
   at "No access".

   | Permission | Access | Used for |
   | --- | --- | --- |
   | **Contents** | Read and write | Pushing `develop`, the `gh-pages` site branch and release tags |
   | **Pull requests** | Read and write | Opening the release pull request (`develop` → `main`) |
   | **Pages** | Read-only | Checking that the preview site is enabled |
   | **Metadata** | Read-only | Set automatically by GitHub |

   Do **not** grant "Administration". Without it, the App cannot change the protection on
   `main` configured in step 6.

4. **Where can this GitHub App be installed?** → **Only on this account**.
5. Click **Create GitHub App**.

## 3. Record the App ID

The App's page shows **App ID: 1234567** near the top. Record it:

```bash
ms github setup --app-id 1234567
```

## 4. Create the private key

1. On the same page, under **Private keys**, click **Generate a private key**. The browser
   downloads a `*.private-key.pem` file.
2. Move it to the expected location and restrict its permissions:

   ```bash
   mv ~/Downloads/*.private-key.pem ~/.config/master-system/github-app.pem
   chmod 600 ~/.config/master-system/github-app.pem
   ```

The key is the App's credential. Never commit or share it. If it leaks, delete it on the
App's page and generate a new one.

## 5. Install the App on your repository only

1. On the App's page, click **Install App**, then **Install** next to your account.
2. Choose **Only select repositories** and select your app's repository only.
3. Click **Install**.

## 6. Protect `main` and `develop` with rulesets

1. Open `https://github.com/<you>/example-app/settings/rules` (repository → **Settings** →
   **Rules** → **Rulesets**).
2. **New ruleset** → **New branch ruleset**:

   **Ruleset 1: protect main**

   | Field | Value |
   | --- | --- |
   | Ruleset name | `protect main` |
   | Enforcement status | **Active** |
   | Bypass list | **Empty** (not even the App) |
   | Target branches | **Add target** → **Include by pattern** → `main` |
   | Restrict deletions | Ticked |
   | Require a pull request before merging | Ticked; required approvals `0` if you are the only reviewer |
   | Block force pushes | Ticked |

3. **New ruleset** → **New branch ruleset** again:

   **Ruleset 2: develop**

   | Field | Value |
   | --- | --- |
   | Ruleset name | `develop` |
   | Enforcement status | **Active** |
   | Target branches | **Include by pattern** → `develop` |
   | Restrict deletions | Ticked |
   | Block force pushes | Ticked |

   The system only fast-forwards `develop`, which this ruleset allows.

With these rules nobody, including the App, can push to `main`. `main` changes only when a
human merges a release pull request.

## 7. Enable GitHub Pages

Skip this step if the project has no `github.site_dir` (no preview site).

The first publish (after a run, or `ms publish <project>`) creates the `gh-pages` branch.
Only a repository administrator can enable Pages, and the App deliberately is not one, so
this is done once by hand:

1. Open `https://github.com/<you>/example-app/settings/pages`.
2. **Source:** "Deploy from a branch". **Branch:** `gh-pages`, folder `/ (root)`. **Save**.

The first build takes a minute or two. Afterwards:

- released version: `https://<you>.github.io/example-app/`
- preview of `develop`: `https://<you>.github.io/example-app/develop/`

Both URLs are stable. GitHub Pages is free for public repositories; private repositories
require a paid plan.

## 8. Verify

```bash
ms github check
```

Every line should read `[ok]`. A `[!!]` line names the step to revisit. The Pages line
shows `[!!]` until step 7 is complete.
