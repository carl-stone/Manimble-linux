"""Private subprocess entry point for PythonPlot; never import into the editor."""
import os
import runpy
import sys
import traceback


def main():
    script, out = sys.argv[1:3]
    sys.argv = [script]
    sys.path.insert(0, os.path.dirname(os.path.abspath(script)))
    try:
        runpy.run_path(script, run_name="__main__")
    except Exception as exc:
        tb = exc.__traceback__
        while tb is not None and tb.tb_frame.f_code.co_filename != script:
            tb = tb.tb_next
        traceback.print_exception(type(exc), exc, tb or exc.__traceback__)
        return 1
    plt = sys.modules.get("matplotlib.pyplot")
    if not os.path.exists(out) and plt is not None and plt.get_fignums():
        plt.gcf().savefig(out, dpi=200, bbox_inches="tight")
    if not os.path.exists(out):
        sys.exit("The code ran but drew nothing: plot with matplotlib, or save a PNG to os.environ['MANIMBLE_PLOT_OUT'].")
    return 0


if __name__ == "__main__":
    sys.exit(main())
