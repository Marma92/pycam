from picamera import PiCamera
from time import sleep

cam = PiCamera()

cam.annotate_text = "BONJOUR MONDE"
cam.start_preview()
sleep(20)
cam.stop_preview()

# v1 specs :
# start the camera in manual mode :
#   - Ability to set speed and ISOs
#   - Ability to set white balance
# Aperture priority mode :
#   - Aperture-based automatic shutter speed
# Settings:
#   - Ability to set image resolution
#   - Ability to set path
# natural previewer (no ATH)
# detailed previewer (with ATH)
# ez gallery ?