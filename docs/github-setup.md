# GitHub setup for the Master System

This guide is for the owner and takes about 15 minutes, all in the browser plus three
commands. At the end:

- the system has its own GitHub identity, a **GitHub App** that can see only Match Legends;
- it can push `develop` and the preview site, but it **can never change `main`**;
- the preview is at <https://f0rrel.github.io/Match_Legends_mobile_game/develop/>.

You can check your progress at any time with `ms github check`. It lists what is done and
what is still missing.

---

## 1. Why an App (and not your password or a token)

A GitHub App is a separate "robot" account that **you** control.
- You choose exactly which repository it can see (only Match Legends) and what it may do.
- Its passwords (tokens) last one hour and are created on demand from a private key that
  stays on your PC.
- If you ever want to stop it, you uninstall the app and it loses all access immediately.

## 2. Create the app

1. Open <https://github.com/settings/apps/new>. This is the same as: your profile picture
   (top right) → **Settings** → **Developer settings** (bottom of the left menu) →
   **GitHub Apps** → **New GitHub App**.
2. Fill in the form. Leave every field not mentioned here as it is.

   | Field | Value |
   | --- | --- |
   | **GitHub App name** | `f0rrel-master-system` (if the name is taken, add something, e.g. `-bot`) |
   | **Homepage URL** | `https://github.com/f0rrel/master-system` |
   | **Webhook → Active** | **untick** this box. The system asks GitHub itself; nothing calls it. |

3. Scroll to **Permissions → Repository permissions** and set exactly these four. Every other
   permission stays at "No access".

   | Permission | Access | Why |
   | --- | --- | --- |
   | **Contents** | Read and write | push `develop`, the site branch `gh-pages`, and release tags |
   | **Pull requests** | Read and write | open the release pull request (`develop` → `main`) |
   | **Pages** | Read-only | check that the preview site is on |
   | **Metadata** | Read-only | GitHub sets this one automatically |

   Do **not** give it "Administration". Without it, the app cannot change the protection on
   `main` that you set in step 6.

4. **Where can this GitHub App be installed?** Choose **Only on this account**.
5. Click **Create GitHub App**.

## 3. Note the App ID

On the page that opens, near the top, there is **App ID: 1234567** (a number). In a terminal:

```bash
ms github setup --app-id 1234567      # your number
```

## 4. Create the private key

1. On the same page, scroll down to **Private keys** and click **Generate a private key**.
   Your browser downloads a file named like `f0rrel-master-system.2026-10-06.private-key.pem`.
2. Move it where the system looks for it, and make it readable only by you:

   ```bash
   mv ~/Downloads/*.private-key.pem ~/.config/master-system/github-app.pem
   chmod 600 ~/.config/master-system/github-app.pem
   ```

Keep this file private: it is the app's password. Never send it to anyone, and never
commit it. If it ever leaks, delete it on the same page and generate a new one.

## 5. Install the app on Match Legends only

1. In the left menu of the app's page, click **Install App**, then **Install** next to your
   account.
2. Choose **Only select repositories** and pick **Match_Legends_mobile_game**. Choose only
   this one.
3. Click **Install**.

## 6. Protect `main` (and `develop`) with rulesets

1. Open <https://github.com/f0rrel/Match_Legends_mobile_game/settings/rules>. This is the
   repository → **Settings** → **Rules** → **Rulesets**.
2. Click **New ruleset** → **New branch ruleset**.

   **Ruleset 1: protect main**

   | Field | Value |
   | --- | --- |
   | Ruleset name | `protect main` |
   | Enforcement status | **Active** |
   | Bypass list | leave **empty** (not even the app) |
   | Target branches | **Add target** → **Include by pattern** → `main` |
   | Rules: Restrict deletions | ticked |
   | Rules: Require a pull request before merging | ticked; required approvals: `0` (you are the only reviewer and you merge yourself) |
   | Rules: Block force pushes | ticked |

   Click **Create**.

3. Click **New ruleset** → **New branch ruleset** again.

   **Ruleset 2: develop**

   | Field | Value |
   | --- | --- |
   | Ruleset name | `develop` |
   | Enforcement status | **Active** |
   | Target branches | **Include by pattern** → `develop` |
   | Rules: Restrict deletions | ticked |
   | Rules: Block force pushes | ticked |

   Click **Create**. The system still pushes `develop` normally (only forward), which this
   allows.

With this, nobody (the app included) can push to `main`. `main` changes only when you
merge a release pull request on GitHub.

## 7. GitHub Pages

The first time the system publishes (after a run, or `ms publish match-legends`), it creates
the `gh-pages` branch. GitHub allows only an admin to switch Pages on, and the app
deliberately isn't one, so you do it once:

1. Open <https://github.com/f0rrel/Match_Legends_mobile_game/settings/pages>.
2. **Source:** "Deploy from a branch". **Branch:** `gh-pages`, folder `/ (root)`. Click
   **Save**.

(For Match Legends this was done on 2026-10-06.) The first build takes a minute or two.
After that:

- the released game: <https://f0rrel.github.io/Match_Legends_mobile_game/>
- the preview of `develop`: <https://f0rrel.github.io/Match_Legends_mobile_game/develop/>

Both links never change. You can open them on a PC or a phone, and add them to your phone's
home screen.

## 8. Check

```bash
ms github check
```

Every line should say `[ok]`. If one says `[!!]`, the line under it tells you which step to
redo. The Pages line shows `[!!]` until step 7 is done.
