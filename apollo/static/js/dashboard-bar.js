  function drawBarChart(el) {
    var margin = {top: 10, right: 40, bottom: 165, left: 0};
    var width = 160 - margin.left - margin.right;
    var height = 320 - margin.top - margin.bottom;
    var barMargin = 3;

    var json = JSON.parse(el.dataset.chart);

    var color = d3.scaleOrdinal().range(["#dc3545", "#ffc107", "#007bff", "#aaaaaa"]); // Conflict color #E83992
    var color_rtl = d3.scaleOrdinal().range(["#aaaaaa", "#007bff", "#ffc107", "#dc3545"]); // Conflict color #E83992
    var labels = ['Missing', 'Partial', 'Complete', 'No Signal']; // Conflict label
    var labels_rtl = ['No Signal', 'Complete', 'Partial', 'Missing']; // Conflict label

    var dataMap = d3.map()
        .set('Missing', json.Missing + (json.Conflict || 0))
        .set('Partial', json.Partial)
        .set('Complete', json.Complete)
        .set('No Signal', json.Offline);
    var total = dataMap.values().reduce(function (prev, curr, idx, arr) { return prev + curr; });

    var y = d3.scaleLinear().range([height, 0]).domain([0, total]);
    var x = d3.scaleBand().rangeRound([0, width]).align(.1);

    if (window.rtl) {
        var yAxis = d3.axisLeft().scale(y).tickValues([0, total]).tickFormat(d3.format('~s'));
    } else {
        var yAxis = d3.axisRight().scale(y).tickValues([0, total]).tickFormat(d3.format('~s'));
    }

    var chart = d3.select(el)
        .append('svg')
        .attr('width', width + margin.right + margin.left)
        .attr('height', height + margin.top + margin.bottom)
        .attr("direction", (window.rtl ? "rtl" : "ltr"))
        .attr('transform', 'translate(' + margin.left + ',' + margin.top + ')');

    var bar = chart.selectAll('g')
        .data(window.rtl ? labels_rtl : labels)
        .enter().append('g')
        .attr('transform', function (d) { return 'translate(' + x.domain(window.rtl ? labels_rtl : labels)(d) + ', 0)'; });

    bar.append('rect')
        .attr('y', function (d) { return y(dataMap.get(d)); })
        .attr('height', function (d) { return height - y(dataMap.get(d)); })
        .attr('transform', 'translate(' + (window.rtl ? margin.right + margin.left : margin.left) + ',' + margin.top + ')')
        .attr('width', x.domain(window.rtl ? labels_rtl : labels)(window.rtl ? labels_rtl[1] : labels[1]) - barMargin)
        .attr('fill', window.rtl ? color_rtl.domain(labels_rtl) : color.domain(labels));

    bar.append('text')
        .attr('x', (x.domain(window.rtl ? labels_rtl : labels)(window.rtl ? labels_rtl[1] : labels[1]) - barMargin) / 2)
        .attr('y', function (d) { return y(dataMap.get(d)) + 4; })
        .attr('dy', "-.85em")
        .attr('fill', '#222')
        .style('font', '8px sans-serif')
        .attr('text-anchor', 'middle')
        .attr('transform', 'translate(' + (window.rtl ? margin.right + margin.left : margin.left) + ',' + margin.top + ')')
        .text(function (d) { return dataMap.get(d); });

    chart.append('g')
        .attr('class', 'y axis')
        .attr('transform', 'translate(' + (window.rtl ? margin.right : (width + margin.left)) + ',' + margin.top + ')')
        .call(yAxis);

    // Legend
    var legend = chart.append('g')
        .attr('transform', 'translate(0, 40)')
        .attr('width', width)
        .attr('height', height)
        .selectAll('g')
        .data(window.rtl ? color_rtl.range() : color.range())
        .enter().append('g')
        .attr('transform', function (d, i) { return "translate(0," + i * 20 + ")"; });

    legend.append('circle')
        .attr('cx', window.rtl ? width + margin.right + margin.left - 5 : 5)
        .attr('cy', height)
        .attr('r', 5)
        .style('fill', color.domain(labels));

    // Legend Label
    legend.append('text')
        .data(labels)
        .attr('x', window.rtl ? width + margin.right + margin.left - 20 : 20)
        .attr('y', height)
        .attr('dy', '.29em')
        .attr('class', 'text-monospace')
        .text(function (d) { return d; });

    // Legend data
    legend.append("text")
        .data(labels)
        .attr("x", window.rtl ? width + margin.right + margin.left - 100 : 100)
        .attr("y", height)
        .attr("dy", ".29em")
        .attr("class", "text-monospace")
        .text(function (d) { return '· ' + dataMap.get(d) });

    var totals = chart.append("g")
        .attr("width", width)
        .attr("height", height)
        .selectAll("g")
        .data(color.range())
        .enter().append("g")
        .attr("transform", function (d, i) { return "translate(0," + i * 20 + ")"; });

    totals.append("text")
        .data(["Total"])
        .attr("x", window.rtl ? width + margin.right + margin.left - 20 : 20)
        .attr("y", height + 40 + (dataMap.values().length + 1) * 16)
        .attr("dy", ".29em")
        .attr('class', 'text-monospace font-weight-bold')
        .text(function (d) { return d });

    totals.append("text")
        .data([total])
        .attr("x", window.rtl ? width + margin.right + margin.left - 100 : 100)
        .attr("y", height + 40 + (dataMap.values().length + 1) * 16)
        .attr("dy", ".29em")
        .attr('class', 'text-monospace font-weight-bold')
        .text(function (d) { return '· ' + d });
  }

  $(function () {
    $('.chart').each(function(idx, el) {
      drawBarChart(el);
    });
  });