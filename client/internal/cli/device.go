package cli

import (
	"fmt"

	"github.com/BCCC0/spark-memory/client/internal/config"
	"github.com/BCCC0/spark-memory/client/internal/devicehome"
	"github.com/BCCC0/spark-memory/client/internal/memerr"
)

func deviceCommand(o options) (any, string, error) {
	if o.sub != "init" {
		return nil, "", memerr.New("usage_error", "use `mem device init`", nil)
	}
	c, err := config.Load(false, true)
	if err != nil {
		return nil, "", err
	}
	if c.Credentials == nil || c.Credentials.Alias == "" {
		return nil, "", memerr.New("unauthorized", "this enrollment has no device alias; run `mem login --alias <stable-name>`", nil)
	}
	target, err := devicehome.Init(c.Credentials.Alias, c.Credentials.DeviceID)
	if err != nil {
		return nil, "", err
	}
	return map[string]any{"path": target, "alias": c.Credentials.Alias, "scope": "device", "share": "never"}, fmt.Sprintf("initialized private device home at %s", target), nil
}
