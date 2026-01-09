import os
import ast

from configparser import RawConfigParser

import sys
import glob
import textwrap
import argparse
import logging

import core.theme

import util.store
import util.format

import modules.core
import modules.contrib

log = logging.getLogger(__name__)

# TOML support - only import when needed
_toml_available = False
try:
    import toml
    _toml_available = True
except ImportError:
    pass

MODULE_HELP = "Specify a space-separated list of modules to load. The order of the list determines their order in the i3bar (from left to right). Use <module>:<alias> to provide an alias in case you want to load the same module multiple times, but specify different parameters."
PARAMETER_HELP = (
    "Provide configuration parameters in the form of <module>.<key>=<value>"
)
THEME_HELP = "Specify the theme to use for drawing modules"


def all_modules():
    """Returns a list of all available modules (either core or contrib)

    :return: list of modules
    :rtype: list of strings
    """
    result = {}

    for path in [modules.core.__file__, modules.contrib.__file__]:
        path = os.path.dirname(path)
        for mod in glob.iglob("{}/*.py".format(path)):
            result[os.path.basename(mod).replace(".py", "")] = 1

    res = list(result.keys())
    res.sort()
    return res


class print_usage(argparse.Action):
    def __init__(self, option_strings, dest, nargs=None, **kwargs):
        argparse.Action.__init__(self, option_strings, dest, nargs, **kwargs)
        self._indent = " " * 2

    def __call__(self, parser, namespace, value, option_string=None):
        if value == "modules":
            self._args = namespace
            self._format = "plain"
            self.print_modules()
        elif value == "modules-rst":
            self._args = namespace
            self._format = "rst"
            self.print_modules()
        elif value == "themes":
            self.print_themes()
        sys.exit(0)

    def print_themes(self):
        print(", ".join(core.theme.themes()))

    def print_modules(self):
        basepath = os.path.abspath(
            os.path.join(os.path.dirname(os.path.realpath(__file__)), "..")
        )

        rst = {}

        if self._format == "rst":
            print(".. THIS DOCUMENT IS AUTO-GENERATED, DO NOT MODIFY")
            print(".. To change this document, please update the docstrings in the individual modules")

        for m in all_modules():
            try:
                module_type = "core"
                filename = os.path.join(basepath, "modules", "core", "{}.py".format(m))
                if not os.path.exists(filename):
                    filename = os.path.join(
                        basepath, "modules", "contrib", "{}.py".format(m)
                    )
                    module_type = "contrib"
                if not os.path.exists(filename):
                    log.warning("module {} not found".format(m))
                    continue

                doc = None
                with open(filename) as f:
                    tree = ast.parse(f.read())
                    doc = ast.get_docstring(tree)

                if not doc:
                    log.warning("failed to find docstring for {}".format(m))
                    continue
                if self._format == "rst":
                    if os.path.exists(
                        os.path.join(basepath, "..", "screenshots", "{}.png".format(m))
                    ):
                        doc = "{}\n\n.. image:: ../screenshots/{}.png".format(doc, m)

                    rst[module_type] = rst.get(module_type, [])
                    rst[module_type].append({"module": m, "content": doc})
                else:
                    print(
                        textwrap.fill(
                            "{}:".format(m),
                            80,
                            initial_indent=self._indent * 2,
                            subsequent_indent=self._indent * 2,
                        )
                    )
                    for line in doc.split("\n"):
                        print(
                            textwrap.fill(
                                line,
                                80,
                                initial_indent=self._indent * 3,
                                subsequent_indent=self._indent * 6,
                            )
                        )
            except Exception as e:
                log.warning(e)

        if self._format == "rst":
            print("List of modules\n===============")
            for k in ["core", "contrib"]:
                print("\n{}\n{}\n".format(k, "-" * len(k)))
                for mod in rst[k]:
                    print("\n{}\n{}\n".format(mod["module"], "~" * len(mod["module"])))
                    print(mod["content"])


class Config(util.store.Store):
    """Represents the configuration of bumblebee-status (either via config file or via CLI)

    :param args: The arguments passed via the commandline
    """

    def __init__(self, args):
        super(Config, self).__init__()

        parser = argparse.ArgumentParser(
            description="bumblebee-status is a modular, theme-able status line generator for the i3 window manager. https://github.com/tobi-wan-kenobi/bumblebee-status/wiki"
        )
        parser.add_argument(
            "-c",
            "--config-file",
            action="store",
            default=None,
            help="Specify a configuration file to use"
        )
        parser.add_argument(
            "--config",
            action="store",
            default=None,
            help="Specify a TOML configuration file to use"
        )
        parser.add_argument(
            "-m", "--modules", nargs="+", action="append", default=[], help=MODULE_HELP
        )
        parser.add_argument(
            "-p",
            "--parameters",
            nargs="+",
            action="append",
            default=[],
            help=PARAMETER_HELP,
        )
        parser.add_argument("-t", "--theme", default=None, help=THEME_HELP)
        parser.add_argument(
            "-i",
            "--iconset",
            default="auto",
            help="Specify the name of an iconset to use (overrides theme default)",
        )
        parser.add_argument(
            "-a",
            "--autohide",
            nargs="+",
            default=[],
            help="Specify a list of modules to hide when not in warning/error state",
        )
        parser.add_argument(
            "-e",
            "--errorhide",
            nargs="+",
            default=[],
            help="Specify a list of modules that are hidden when in state error"
        )
        parser.add_argument(
            "-d", "--debug", action="store_true", help="Add debug fields to i3 output"
        )
        parser.add_argument(
            "-f",
            "--logfile",
            help="destination for the debug log file, if -d|--debug is specified; defaults to stderr",
        )
        parser.add_argument(
            "-r",
            "--right-to-left",
            action="store_true",
            help="Draw widgets from right to left, rather than left to right (which is the default)",
        )
        parser.add_argument(
            "-l",
            "--list",
            choices=["modules", "themes", "modules-rst"],
            help="Display a list of available themes or available modules, along with their parameters",
            action=print_usage,
        )

        self.__args = parser.parse_args(args)

        # Internal storage for TOML-loaded modules
        self.__toml_modules = []
        self.__toml_theme = None
        self.__toml_debug = None

        # Load TOML config if --config is provided
        if self.__args.config:
            toml_path = os.path.expanduser(self.__args.config)
            if not os.path.exists(toml_path):
                log.error("TOML config file not found: {}".format(toml_path))
                raise SystemExit("TOML config file not found: {}".format(toml_path))
            self.load_toml_config(toml_path)

        # Load legacy config file if --config-file is provided
        if self.__args.config_file:
            cfg = self.__args.config_file
            cfg = os.path.expanduser(cfg)
            self.load_config(cfg)
        elif not self.__args.config:
            # Only auto-load legacy config if TOML config wasn't used
            for cfg in [
                "~/.bumblebee-status.conf",
                "~/.config/bumblebee-status.conf",
                "~/.config/bumblebee-status/config",
            ]:
                cfg = os.path.expanduser(cfg)
                self.load_config(cfg)

        # Apply CLI parameter overrides (highest precedence)
        parameters = [item for sub in self.__args.parameters for item in sub]
        for param in parameters:
            if "=" not in param:
                log.error(
                    'missing value for parameter "{}" - ignoring this parameter'.format(
                        param
                    )
                )
                continue
            key, value = param.split("=", 1)
            self.set(key, value)

    """Loads and validates a TOML configuration file

    :param filename: path to the TOML file to load
    :raises SystemExit: if TOML parsing fails or validation errors occur
    """
    def load_toml_config(self, filename):
        if not _toml_available:
            log.error("TOML support requires the 'toml' package. Install it with: pip install toml")
            raise SystemExit("TOML support requires the 'toml' package. Install it with: pip install toml")

        try:
            with open(filename, 'r') as f:
                data = toml.load(f)
        except Exception as e:
            log.error("Failed to parse TOML config file {}: {}".format(filename, e))
            raise SystemExit("Failed to parse TOML config file {}: {}".format(filename, e))

        if "theme" in data:
            self.__toml_theme = data["theme"]

        if "interval" in data:
            self.set("interval", str(data["interval"]))

        if "debug" in data:
            if isinstance(data["debug"], bool):
                self.__toml_debug = data["debug"]
            else:
                log.warning("'debug' in TOML config must be a boolean, ignoring")

        if "autohide" in data:
            if isinstance(data["autohide"], list):
                # Store as comma-separated string to match legacy format
                autohide_list = [str(m) for m in data["autohide"]]
                self.set("autohide", ",".join(autohide_list))
            else:
                log.warning("'autohide' in TOML config must be an array, ignoring")

        if "modules" not in data:
            log.warning("No 'modules' section found in TOML config")
            return

        if not isinstance(data["modules"], list):
            log.error("'modules' must be an array of tables in TOML config")
            raise SystemExit("'modules' must be an array of tables in TOML config")

        # Track module identifiers to detect duplicates
        module_ids = {}
        module_name_counts = {}  # Track how many times each module name appears
        modules_list = []

        for idx, module_entry in enumerate(data["modules"]):
            if not isinstance(module_entry, dict):
                log.error("Module entry at index {} must be a table".format(idx))
                raise SystemExit("Module entry at index {} must be a table".format(idx))

            if "name" not in module_entry:
                log.error("Module entry at index {} is missing required 'name' field".format(idx))
                raise SystemExit("Module entry at index {} is missing required 'name' field".format(idx))

            module_name = module_entry["name"]
            alias = module_entry.get("alias", None)
            params = module_entry.get("params", {})

            if not isinstance(params, dict):
                log.error("Module '{}' at index {} has invalid 'params' (must be a table)".format(module_name, idx))
                raise SystemExit("Module '{}' at index {} has invalid 'params' (must be a table)".format(module_name, idx))

            existing_count = module_name_counts.get(module_name, 0)

            if existing_count > 0 and not alias:
                log.error("Module '{}' appears multiple times (at index {}). Multiple instances require aliases.".format(
                    module_name, idx
                ))
                raise SystemExit("Module '{}' appears multiple times (at index {}). Multiple instances require aliases.".format(
                    module_name, idx
                ))

            module_name_counts[module_name] = existing_count + 1

            if alias:
                module_id = "{}:{}".format(module_name, alias)
            else:
                module_id = module_name

            if module_id in module_ids:
                log.error("Duplicate module identifier '{}' at index {} (first seen at index {}).".format(
                    module_id, idx, module_ids[module_id]
                ))
                raise SystemExit("Duplicate module identifier '{}' at index {}.".format(
                    module_id, idx
                ))

            module_ids[module_id] = idx

            modules_list.append({
                "name": module_name,
                "alias": alias,
                "params": params
            })

            # Apply parameters to config store
            param_prefix = alias if alias else module_name
            for key, value in params.items():
                config_key = "{}.{}".format(param_prefix, key)
                # Convert value to string to match existing behavior
                self.set(config_key, str(value))

        self.__toml_modules = modules_list
        log.info("Loaded {} modules from TOML config".format(len(modules_list)))

    """Loads parameters from an init-style configuration file

    :param filename: path to the file to load
    """

    def load_config(self, filename, content=None):
        if os.path.exists(filename) or content is not None:
            log.info("loading {}".format(filename))
            tmp = RawConfigParser()
            tmp.optionxform = str

            if content:
                tmp.read_string(content)
            else:
                tmp.read(u"{}".format(filename))

            if tmp.has_section("module-parameters"):
                for key, value in tmp.items("module-parameters"):
                    self.set(key, value)
            if tmp.has_section("core"):
                for key, value in tmp.items("core"):
                    self.set(key, value)


    """Returns a list of configured modules

    Merge order: CLI -m flags override TOML modules, which override legacy config.

    :return: list of configured (active) modules in format "module" or "module:alias"
    :rtype: list of strings
    """

    def modules(self):
        # CLI -m flags have highest precedence (replace module list)
        list_of_modules = [item for sub in self.__args.modules for item in sub]

        if list_of_modules == []:
            # If no CLI modules, use TOML modules if available
            if self.__toml_modules:
                list_of_modules = []
                for module_def in self.__toml_modules:
                    if module_def["alias"]:
                        list_of_modules.append("{}:{}".format(module_def["name"], module_def["alias"]))
                    else:
                        list_of_modules.append(module_def["name"])
            else:
                # Fall back to legacy config format
                list_of_modules = util.format.aslist(self.get('modules', []))
        return list_of_modules

    """Returns the global update interval

    :return: update interval in seconds
    :rtype: float
    """

    def interval(self, default=1):
        return util.format.seconds(self.get("interval", default))

    """Returns the global popup menu font size

    :return: popup menu font size
    :rtype: int
    """

    def popup_font_size(self, default=12):
        return util.format.asint(self.get("popup_font_size", default))

    """Returns whether debug mode is enabled

    Merge order: CLI -d flag overrides TOML debug setting.

    :return: True if debug is enabled, False otherwise
    :rtype: boolean
    """

    def debug(self):
        if self.__args.debug:
            return True
        if self.__toml_debug is not None:
            return self.__toml_debug
        return False

    """Returns whether module order should be reversed/inverted

    :return: True if modules should be reversed, False otherwise
    :rtype: boolean
    """

    def reverse(self):
        return self.__args.right_to_left

    """Returns the logfile location

    :return: location where the logfile should be written
    :rtype: string
    """

    def logfile(self):
        return self.__args.logfile

    """Returns the configured theme name

    Merge order: CLI -t flag overrides TOML theme, which overrides legacy config.

    :return: name of the configured theme
    :rtype: string
    """

    def theme(self):
        if self.__args.theme:
            return self.__args.theme
        if self.__toml_theme:
            return self.__toml_theme
        # Fall back to legacy config or default
        return self.get("theme") or "default"

    """Returns the configured iconset name

    :return: name of the configured iconset
    :rtype: string
    """

    def iconset(self):
        return self.__args.iconset

    """Returns whether a module should be hidden if their state is not warning/critical

    :return: True if module should be hidden automatically, False otherwise
    :rtype: bool
    """

    def autohide(self, name):
        return name in self.__args.autohide or name in util.format.aslist(self.get("autohide", []))

    """Returns which modules should be hidden if they are in state error

    :return: returns True if name should be hidden, False otherwise
    :rtype: bool
    """
    def errorhide(self, name):
        return name in self.__args.errorhide

# vim: tabstop=8 expandtab shiftwidth=4 softtabstop=4
