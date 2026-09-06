# Render every case part to STL.
#
#   make          all parts
#   make check    verify each renders as a closed manifold
#   make clean

PARTS := test_fit base yoke head
STLS  := $(addprefix stl/,$(addsuffix .stl,$(PARTS)))

OPENSCAD ?= openscad

.PHONY: all check clean $(PARTS)

all: $(STLS)

stl:
	mkdir -p stl

stl/%.stl: %.scad params.scad | stl
	$(OPENSCAD) -o $@ $<

# Convenience: `make head` rather than `make stl/head.stl`.
$(PARTS): %: stl/%.stl

# OpenSCAD reports "Simple: yes" only for a closed, self-consistent solid.
# A part that fails this slices into something with holes in it.
#
# Rendered to a real file, not /dev/null: with /dev/null OpenSCAD skips the
# geometry summary entirely and the grep below silently matches nothing,
# which made this check pass on anything.
check: $(STLS)
	@for part in $(PARTS); do \
		if $(OPENSCAD) -o $$part.check.stl $$part.scad 2>&1 \
			| grep -qE 'Simple:[[:space:]]+yes'; then \
			echo "  $$part: closed manifold"; \
		else \
			echo "  $$part: NOT MANIFOLD"; rm -f $$part.check.stl; exit 1; \
		fi; \
		rm -f $$part.check.stl; \
	done
	@echo "All parts are closed manifolds."

clean:
	rm -rf stl
