# Example invocation of the step2c cloud editor for Windows.
# Edit the paths below to match your project.

$ErrorActionPreference = "Stop"

$IMAGE = "input_drawing.jpg"
$CLOUDS = "output\outer_clouds_v2.json"
$OVERLAY = "output\overlay_v2.jpg"
$OUTDIR = "output"

python step2c_cloud_editor.py --image $IMAGE --clouds $CLOUDS --overlay $OVERLAY --out $OUTDIR
