package cli

import (
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/url"
	"os"
	"sort"
	"strconv"
	"strings"
	"time"

	"github.com/BCCC0/spark-memory/client/internal/config"
	"github.com/BCCC0/spark-memory/client/internal/memerr"
	"github.com/BCCC0/spark-memory/client/internal/pyrepr"
	"github.com/BCCC0/spark-memory/client/internal/transport"
)

type options struct {
	command, sub string
	explicit     bool
	positional   []string
	extras       []string
	flags        map[string]string
	human        bool
	server       string
}

type flagKind int

const (
	boolFlag flagKind = iota
	valueFlag
)

type commandSpec struct {
	name     string
	flags    map[string]flagKind
	short    map[byte]string
	min, max int
	required []string
	subs     map[string]*commandSpec
	subOrder []string
}

func cmd(name string, min, max int, required []string, flags map[string]flagKind) *commandSpec {
	return &commandSpec{name: name, flags: flags, min: min, max: max, required: required}
}

func withSubs(name string, order []string, subs map[string]*commandSpec) *commandSpec {
	return &commandSpec{name: name, max: 0, subs: subs, subOrder: order}
}

var commandSpecs = func() map[string]*commandSpec {
	addFlags := map[string]flagKind{"pointer": boolFlag, "as": valueFlag, "expect": valueFlag, "from-git": valueFlag, "message": valueFlag}
	add := cmd("add", 1, -1, []string{"files"}, addFlags)
	add.short = map[byte]string{'m': "message"}
	rm := cmd("rm", 1, 1, []string{"id"}, map[string]flagKind{"expect": valueFlag, "message": valueFlag})
	rm.short = map[byte]string{'m': "message"}
	mv := cmd("mv", 2, 2, []string{"source", "destination"}, map[string]flagKind{"dry-run": boolFlag, "expect-head": valueFlag, "message": valueFlag})
	mv.short = map[byte]string{'m': "message"}
	pointerAdd := cmd("add", 2, 2, []string{"expression", "id"}, map[string]flagKind{"message": valueFlag})
	pointerAdd.short = map[byte]string{'m': "message"}
	hookAdd := cmd("add", 1, -1, []string{"patterns"}, nil)
	hookRm := cmd("rm", 1, -1, []string{"patterns"}, nil)
	return map[string]*commandSpec{
		"login":    cmd("login", 0, 0, nil, map[string]flagKind{"name": valueFlag, "alias": valueFlag, "secret": valueFlag}),
		"logout":   cmd("logout", 0, 0, nil, nil),
		"init":     cmd("init", 0, 1, nil, map[string]flagKind{"summary": valueFlag}),
		"status":   cmd("status", 0, 0, nil, nil),
		"flush":    cmd("flush", 0, 0, nil, nil),
		"projects": cmd("projects", 0, 0, nil, nil),
		"manifest": cmd("manifest", 0, 1, nil, nil),
		"device":   withSubs("device", []string{"init"}, map[string]*commandSpec{"init": cmd("init", 0, 0, nil, nil)}),
		"ls": cmd("ls", 0, 1, nil, map[string]flagKind{
			"status": valueFlag, "type": valueFlag, "tag": valueFlag, "since": valueFlag,
			"sort": valueFlag, "limit": valueFlag, "cursor": valueFlag, "full": boolFlag,
		}),
		"read": cmd("read", 1, -1, []string{"ids"}, map[string]flagKind{"revision": valueFlag, "outline": boolFlag}),
		"devices": cmd("devices", 0, 0, nil, map[string]flagKind{
			"retire": valueFlag,
		}),
		"add": add,
		"pointer": withSubs("pointer", []string{"add"}, map[string]*commandSpec{
			"add": pointerAdd,
		}),
		"rm": rm,
		"mv": mv,
		"search": cmd("search", 1, 1, []string{"query"}, map[string]flagKind{
			"mode": valueFlag, "project": valueFlag, "status": valueFlag, "type": valueFlag,
			"tag": valueFlag, "limit": valueFlag,
		}),
		"index": cmd("index", 0, 0, nil, map[string]flagKind{"rebuild": boolFlag}),
		"hook": withSubs("hook", []string{"add", "rm", "ls", "init", "update", "check"}, map[string]*commandSpec{
			"add":  hookAdd,
			"rm":   hookRm,
			"ls":   cmd("ls", 0, 0, nil, nil),
			"init": cmd("init", 0, 0, nil, map[string]flagKind{"uninstall": boolFlag}),
			"update": cmd("update", 0, 0, nil, map[string]flagKind{
				"repo": valueFlag, "rev": valueFlag, "dry-run": boolFlag,
			}),
			"check": cmd("check", 0, 0, nil, map[string]flagKind{"repo": valueFlag}),
		}),
	}
}()

func rootChoices() string {
	// argparse lists commands in declaration order.
	order := []string{"login", "logout", "init", "status", "flush", "projects", "manifest", "device", "ls", "read", "devices", "add", "pointer", "rm", "mv", "search", "index", "hook"}
	out := make([]string, 0, len(order))
	for _, name := range order {
		if _, ok := commandSpecs[name]; ok {
			out = append(out, name)
		}
	}
	return strings.Join(out, ", ")
}

// currentSpec is the parser that owns the next option or positional.
func currentSpec(o *options) *commandSpec {
	if o.command == "" {
		return nil
	}
	spec := commandSpecs[o.command]
	if spec == nil {
		return nil
	}
	if o.sub != "" && spec.subs != nil {
		return spec.subs[o.sub]
	}
	return spec
}

func currentKey(o *options) string {
	if o.command == "" {
		return "root"
	}
	if o.sub != "" {
		return o.command + " " + o.sub
	}
	return o.command
}

func specFlags(spec *commandSpec) map[string]flagKind {
	flags := map[string]flagKind{"help": boolFlag, "human": boolFlag, "server": valueFlag}
	if spec != nil {
		for name, kind := range spec.flags {
			flags[name] = kind
		}
	}
	return flags
}

func resolveLong(spec *commandSpec, name string) (string, flagKind, error) {
	flags := specFlags(spec)
	if kind, ok := flags[name]; ok {
		return name, kind, nil
	}
	var matches []string
	for candidate := range flags {
		if strings.HasPrefix(candidate, name) {
			matches = append(matches, candidate)
		}
	}
	sort.Strings(matches)
	switch len(matches) {
	case 0:
		return "", 0, nil
	case 1:
		return matches[0], flags[matches[0]], nil
	default:
		quoted := make([]string, len(matches))
		for i, match := range matches {
			quoted[i] = "--" + match
		}
		return "", 0, fmt.Errorf("ambiguous option: --%s could match %s", name, strings.Join(quoted, ", "))
	}
}

func (o *options) addExtra(token string) {
	o.extras = append(o.extras, token)
}

func parse(args []string) (options, error) {
	o := options{flags: map[string]string{}}
	endOptions := false
	for i := 0; i < len(args); i++ {
		a := args[i]
		if !endOptions && a == "--" {
			endOptions = true
			continue
		}
		if !endOptions && (a == "-h" || a == "--help") {
			o.flags["help"] = "true"
			return o, nil
		}
		if !endOptions && a == "--version" {
			o.flags["version"] = "true"
			return o, nil
		}
		if !endOptions && strings.HasPrefix(a, "-") && a != "-" {
			if !strings.HasPrefix(a, "--") {
				spec := currentSpec(&o)
				short := ""
				if spec != nil && spec.short != nil {
					short = spec.short['m']
				}
				if short != "" && (a == "-m" || strings.HasPrefix(a, "-m")) {
					if a == "-m" {
						if i+1 >= len(args) {
							return o, usageError(currentKey(&o), "argument -m/--message: expected one argument")
						}
						o.flags[short] = args[i+1]
						i++
					} else {
						o.flags[short] = a[2:]
					}
					continue
				}
				o.addExtra(a)
				continue
			}
			if strings.HasPrefix(a, "--") {
				name, value, hasValue := strings.Cut(strings.TrimPrefix(a, "--"), "=")
				spec := currentSpec(&o)
				canon, kind, err := resolveLong(spec, name)
				if err != nil {
					return o, usageError(currentKey(&o), err.Error())
				}
				if canon == "" {
					o.addExtra(a)
					continue
				}
				if kind == boolFlag {
					if hasValue {
						return o, usageError(currentKey(&o), "argument --"+canon+": ignored explicit argument "+pyrepr.String(value))
					}
					o.flags[canon] = "true"
					continue
				}
				if !hasValue {
					if i+1 >= len(args) {
						return o, usageError(currentKey(&o), "argument --"+canon+": expected one argument")
					}
					next := args[i+1]
					negative := canon == "limit" && isInteger(next)
					if strings.HasPrefix(next, "-") && !negative {
						return o, usageError(currentKey(&o), "argument --"+canon+": expected one argument")
					}
					value = next
					i++
				}
				o.flags[canon] = value
				continue
			}
			o.addExtra(a)
			continue
		}
		if o.command == "" {
			if _, ok := commandSpecs[a]; !ok {
				return o, usageError("root", "argument command: invalid choice: "+pyrepr.String(a)+" (choose from "+rootChoices()+")")
			}
			o.command = a
			o.explicit = true
			continue
		}
		spec := currentSpec(&o)
		if spec != nil && len(spec.subs) > 0 && o.sub == "" {
			if _, ok := spec.subs[a]; !ok {
				label := o.command + "_command"
				return o, usageError(o.command, "argument "+label+": invalid choice: "+pyrepr.String(a)+" (choose from "+strings.Join(spec.subOrder, ", ")+")")
			}
			o.sub = a
			continue
		}
		o.positional = append(o.positional, a)
	}
	if o.command == "" {
		o.command = "status"
	}
	spec := currentSpec(&o)
	key := currentKey(&o)
	if len(o.positional) < spec.min {
		return o, usageError(key, "the following arguments are required: "+strings.Join(spec.required[len(o.positional):], ", "))
	}
	if spec.max >= 0 && len(o.positional) > spec.max {
		for _, extra := range o.positional[spec.max:] {
			o.addExtra(extra)
		}
		o.positional = o.positional[:spec.max]
	}
	if len(o.extras) > 0 {
		// argparse reports unrecognized arguments from the top-level parser,
		// even when the offending token follows a subcommand.
		return o, usageError("root", "unrecognized arguments: "+strings.Join(o.extras, " "))
	}
	if value := o.flag("sort"); value != "" && value != "updated" && value != "id" {
		return o, usageError(key, "argument --sort: invalid choice: "+pyrepr.String(value)+" (choose from updated, id)")
	}
	if value := o.flag("mode"); value != "" && value != "text" && value != "hybrid" {
		return o, usageError(key, "argument --mode: invalid choice: "+pyrepr.String(value)+" (choose from text, hybrid)")
	}
	if value := o.flag("limit"); value != "" {
		if _, err := strconv.Atoi(value); err != nil {
			return o, usageError(key, "argument --limit: invalid int value: "+pyrepr.String(value))
		}
	}
	o.human = o.flags["human"] == "true"
	o.server = o.flags["server"]
	return o, nil
}

func isInteger(value string) bool {
	_, err := strconv.Atoi(value)
	return err == nil
}

func (o options) flag(key string) string { return o.flags[key] }
func (o options) has(key string) bool    { return o.flags[key] == "true" }
func (o options) one() (string, error) {
	if len(o.positional) != 1 {
		return "", memerr.New("usage_error", o.command+" requires one argument", nil)
	}
	return o.positional[0], nil
}
func (o options) count(min, max int) error {
	if len(o.positional) < min || (max >= 0 && len(o.positional) > max) {
		return memerr.New("usage_error", fmt.Sprintf("%s expects %d to %d arguments", o.command, min, max), nil)
	}
	return nil
}

func Run(args []string) int {
	o, err := parse(args)
	if err != nil {
		return fail(err, false)
	}
	if o.has("version") {
		printVersion()
		return 0
	}
	if o.has("help") {
		printHelp(o)
		return 0
	}
	var result any
	var human string
	switch o.command {
	case "login":
		result, human, err = login(o)
	case "logout":
		result, human, err = logout(o)
	case "init":
		result, human, err = initProject(o)
	case "status", "flush", "projects", "manifest", "ls", "read", "search", "index", "devices":
		result, human, err = readCommand(o)
	case "add", "pointer", "rm", "mv":
		result, human, err = writeCommand(o)
	case "hook":
		result, human, err = hookCommand(o)
	case "device":
		result, human, err = deviceCommand(o)
	default:
		err = memerr.New("usage_error", "unknown command: "+o.command, nil)
	}
	if err != nil {
		if result != nil {
			if o.human && human != "" {
				fmt.Fprintln(os.Stdout, human)
			} else {
				data, marshalErr := json.Marshal(result)
				if marshalErr == nil {
					fmt.Fprintln(os.Stdout, string(data))
				}
			}
		}
		return fail(err, o.human)
	}
	if o.human && human != "" {
		fmt.Fprintln(os.Stdout, human)
	} else {
		data, e := json.Marshal(result)
		if e != nil {
			return fail(e, o.human)
		}
		fmt.Fprintln(os.Stdout, string(data))
	}
	return 0
}
func fail(err error, human bool) int {
	var e *memerr.Error
	if !errors.As(err, &e) {
		e = memerr.New("internal_error", err.Error(), nil)
	}
	if human && e.Code != "usage_error" {
		fmt.Fprintf(os.Stderr, "mem: %s: %s\n", e.Code, e.Message)
	} else {
		data, _ := json.Marshal(map[string]any{"error": e})
		fmt.Fprintln(os.Stderr, string(data))
	}
	return e.ExitCode()
}
func client(o options, c config.Config, timeout time.Duration) (*transport.Client, error) {
	server := c.Server
	if o.server != "" {
		server = o.server
	}
	token := ""
	if c.Credentials != nil {
		token = c.Credentials.Token
	}
	return transport.New(server, token, timeout)
}
func setup(o options, project bool, timeout time.Duration) (config.Config, *transport.Client, error) {
	c, err := config.Load(project, true)
	if err != nil {
		return c, nil, err
	}
	cl, err := client(o, c, timeout)
	return c, cl, err
}
func query(values map[string]string) url.Values {
	q := url.Values{}
	for key, value := range values {
		if value != "" {
			q.Set(key, value)
		}
	}
	return q
}
func req(cl *transport.Client, method, path string, q map[string]string, body any, headers http.Header) (any, error) {
	return cl.JSON(method, path, query(q), body, headers)
}
func object(value any) map[string]any {
	if v, ok := value.(map[string]any); ok {
		return v
	}
	return map[string]any{}
}
func str(value any) string {
	if value == nil {
		return ""
	}
	return fmt.Sprint(value)
}
func stringField(value any, key string) string { return str(object(value)[key]) }
func positiveInt(raw string, def int) (int, error) {
	if raw == "" {
		return def, nil
	}
	value, err := strconv.Atoi(raw)
	if err != nil {
		return 0, memerr.New("usage_error", "invalid integer: "+raw, nil)
	}
	return value, nil
}
