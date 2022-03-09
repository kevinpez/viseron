"""Image classification constants."""

DOMAIN = "image_classification"


# Event topic constants
EVENT_IMAGE_CLASSIFICATION_RESULT = "{camera_identifier}/image_classification/result"
EVENT_IMAGE_CLASSIFICATION_EXPIRED = "{camera_identifier}/image_classification/expired"


# BASE_CONFIG_SCHEMA constants
CONFIG_CAMERAS = "cameras"
CONFIG_EXPIRE_AFTER = "expire_after"

DEFAULT_EXPIRE_AFTER = 5
