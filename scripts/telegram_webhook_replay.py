from __future__ import annotations

from scripts import telegram_webhook_replay_core as core


def __getattr__(name: str):
    return getattr(core, name)


def _install_v5_after_active() -> None:
    if getattr(core.active, "_isco_v5_replay_hooked", False):
        return
    original_install = core.active._install

    def install_with_v5() -> None:
        original_install()
        from scripts import telegram_creator_control_center_v5 as creator_v5

        creator_v5.install()

    core.active._install = install_with_v5
    core.active._isco_v5_replay_hooked = True


def replay_update(state_path, update):
    _install_v5_after_active()
    return core.replay_update(state_path, update)


def main() -> None:
    _install_v5_after_active()
    core.main()


if __name__ == "__main__":
    main()
