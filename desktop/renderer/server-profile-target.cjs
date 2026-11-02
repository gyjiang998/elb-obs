'use strict';

// Renderer and Main Process intentionally share one canonical target identity.
// Keeping this tiny compatibility entry point avoids changing the renderer API.
module.exports = require('../runtime/server-profile-target.cjs');
