package cli

import (
	"bufio"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"unicode"

	"golang.org/x/term"

	"github.com/BCCC0/spark-memory/client/internal/config"
	"github.com/BCCC0/spark-memory/client/internal/hostinfo"
	"github.com/BCCC0/spark-memory/client/internal/ids"
	"github.com/BCCC0/spark-memory/client/internal/keys"
	"github.com/BCCC0/spark-memory/client/internal/memerr"
	"github.com/BCCC0/spark-memory/client/internal/transport"
)

func defaultAlias(host string) string {
	host = strings.ToLower(host)
	var b strings.Builder
	for _, r := range host {
		if unicode.IsLetter(r) && r < 128 || unicode.IsDigit(r) && r < 128 {
			b.WriteRune(r)
		} else if b.Len() > 0 && !strings.HasSuffix(b.String(), "-") {
			b.WriteByte('-')
		}
	}
	value := strings.Trim(b.String(), "-")
	if len(value) > 40 {
		value = strings.Trim(value[:40], "-")
	}
	if value == "" {
		return "device"
	}
	return value
}
func login(o options) (any, string, error) {
	if err := o.count(0, 0); err != nil {
		return nil, "", err
	}
	server, err := config.LoginServer()
	if err != nil {
		return nil, "", err
	}
	if o.server != "" {
		server = o.server
	}
	host, _ := os.Hostname()
	name := o.flag("name")
	if name == "" {
		name = host
	}
	alias := o.flag("alias")
	if alias == "" {
		alias = defaultAlias(host)
	}
	secret := o.flag("secret")
	if secret == "" {
		secret = os.Getenv("MEM_ENROLL_SECRET")
	}
	if secret == "" {
		fmt.Fprint(os.Stderr, "enrollment secret: ")
		if term.IsTerminal(int(os.Stdin.Fd())) {
			bytes, readErr := term.ReadPassword(int(os.Stdin.Fd()))
			fmt.Fprintln(os.Stderr)
			if readErr != nil {
				return nil, "", memerr.New("internal_error", "cannot read enrollment secret: "+readErr.Error(), nil)
			}
			secret = string(bytes)
		} else {
			line, readErr := bufio.NewReader(os.Stdin).ReadString('\n')
			if readErr != nil && line == "" {
				return nil, "", memerr.New("internal_error", "cannot read enrollment secret: "+readErr.Error(), nil)
			}
			secret = strings.TrimRight(line, "\r\n")
		}
	}
	cl, err := transport.New(server, "", 20e9)
	if err != nil {
		return nil, "", err
	}
	result, err := req(cl, "POST", "/v1/enroll", nil, map[string]any{"display_name": name, "enroll_secret": secret, "alias": alias, "hostname": host, "os": hostinfo.OS(), "arch": hostinfo.Machine()}, nil)
	if err != nil {
		return nil, "", err
	}
	r := object(result)
	creds := config.Credentials{Server: server, DeviceID: str(r["device_id"]), DisplayName: str(r["display_name"]), Alias: str(r["alias"]), Token: str(r["token"])}
	if creds.DisplayName == "" {
		creds.DisplayName = name
	}
	path, err := config.WriteCredentials(creds)
	if err != nil {
		return nil, "", err
	}
	if err := config.WriteConfig(server); err != nil {
		return nil, "", err
	}
	output := map[string]any{"server": server, "device_id": creds.DeviceID, "display_name": creds.DisplayName, "alias": creds.Alias, "credentials": path}
	return output, fmt.Sprintf("enrolled %s [%s] (%s) at %s", creds.DeviceID, creds.Alias, creds.DisplayName, server), nil
}
func logout(o options) (any, string, error) {
	removed, err := config.DeleteCredentials()
	if err != nil {
		return nil, "", err
	}
	human := "no local token to delete"
	if removed {
		human = "local token deleted"
	}
	return map[string]any{"removed": removed}, human, nil
}
func initProject(o options) (any, string, error) {
	if err := o.count(0, 1); err != nil {
		return nil, "", err
	}
	c, cl, err := setup(o, false, 20e9)
	if err != nil {
		return nil, "", err
	}
	wd, err := os.Getwd()
	if err != nil {
		return nil, "", err
	}
	project := filepath.Base(wd)
	if len(o.positional) > 0 {
		project = o.positional[0]
	}
	project, err = ids.Project(project)
	if err != nil {
		return nil, "", err
	}
	summaryText := o.flag("summary")
	if summaryText == "" {
		summaryText = project + " project"
	}
	marker := filepath.Join(wd, ".mem.toml")
	if _, err := os.Stat(marker); err == nil {
		return map[string]any{"project": project, "marker": marker, "created": false}, marker + " already exists — left unchanged", nil
	}
	rows, err := req(cl, "GET", "/v1/projects", nil, nil, nil)
	if err != nil {
		return nil, "", err
	}
	known := false
	if values, ok := rows.([]any); ok {
		for _, value := range values {
			if stringField(value, "project") == project {
				known = true
			}
		}
	}
	created := false
	receipt := ""
	if !known {
		title := project + " — project"
		quoted, _ := json.Marshal(summaryText)
		body := fmt.Sprintf("---\nsummary: %s\nstatus: active\ntype: project\n---\n\n# %s\n\n## Scope\n\n%s\n", string(quoted), title, summaryText)
		origin := filepath.Join(wd, "PROJECT.md")
		message := "mem: create project " + project
		fields := map[string]string{"project": project, "path": "PROJECT", "kind": "note", "origin_path": origin, "message": message}
		response, err := cl.Multipart("/v1/add", fields, "PROJECT.md", []byte(body), headers(keys.Add(digest([]byte(body)), "PROJECT", "", origin, message), ""))
		if err != nil {
			return nil, "", err
		}
		receipt = stringField(response, "receipt")
		created = true
	}
	marker, err = config.WriteMarker(wd, project)
	if err != nil {
		return nil, "", err
	}
	output := map[string]any{"project": project, "marker": marker, "created": created}
	if receipt != "" {
		output["status"] = "queued"
		output["receipt"] = receipt
	}
	_ = c
	status := "already on the server"
	if created {
		status = "created"
	}
	return output, fmt.Sprintf("project %s %s; wrote %s", project, status, marker), nil
}
